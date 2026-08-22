"""
MCP server exposing this tool's plan/build/validate/deploy pipeline to a
local MCP client (Claude Code, Claude Desktop, VS Code) over stdio.

    python mcp_server.py

Runs in-process against the same planner/generator/sfdc code server.py's web
UI uses - no HTTP hop, no separate FastAPI process required. Reuses
server.py's own helpers (build_provider, _bundle_files_and_types, _failures)
rather than re-implementing them, so this stays in lockstep with the web UI
instead of drifting into a second, slightly-different pipeline.

Two credential sources, on purpose kept separate (see the project's own
notes on this):
  - LLM provider key: server-side default only for now (no api_key param) -
    build_provider() falls back to GEMINI_API_KEY / ANTHROPIC_API_KEY /
    OLLAMA_API_KEY from the environment, same as the web UI's own default.
  - Salesforce org credentials: resolved locally through the `sf` CLI
    (flowtool.orgs.get_org), the same path verify_object_apex.py already
    uses. A token never appears in a tool call or this process's stdio
    transport - only an org alias does.

Each tool call runs its work to completion before returning (a build can
mean several sequential LLM calls) rather than exposing server.py's
start/poll job model - MCP tool calls are allowed to take a while, and a
synchronous request/response is a much simpler contract for a calling agent
than a second polling loop layered on top of its own.

State: builds live in an in-memory dict (BUILDS), keyed by a build_id this
process hands back - there is no persistence across restarts, matching
server.py's own SESSIONS/PLAN_SESSIONS (in-memory, per-process).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List

from mcp.server.mcpserver import MCPServer

from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomField, CustomObject
from flowtool.orgs import SfCliError, get_org
from flowtool.planner import Plan, PlannerGenerator, StepResult, execute_plan, refine_step
from flowtool.report import render_html_fragment
from flowtool.sfdc import component_setup_url, validate_bundle
from server import _bundle_files_and_types, _failures, build_provider

mcp = MCPServer(
    name="sfdc-flow-forge",
    instructions=(
        "Turn a natural-language Salesforce implementation request into "
        "Custom Objects, Custom Fields, Apex Classes and Flows, review them, "
        "then validate/deploy against a real org. Call build first, review "
        "its output, then validate before ever calling deploy - deploy "
        "changes a real Salesforce org."
    ),
)


@dataclass
class Build:
    steps: List[StepResult]
    # Same optimistic-concurrency gate as PlanSession in server.py: version
    # starts at 1, approved_version at 0, so a fresh build is unapproved.
    # approve(build_id, version) only takes effect if `version` still matches
    # - a revise() in between bumps `version` without touching
    # approved_version, which is what makes approved() false again without a
    # separate reset step.
    version: int = 1
    approved_version: int = 0

    @property
    def approved(self) -> bool:
        return self.approved_version == self.version


BUILDS: Dict[str, Build] = {}


def _step_summary(result: StepResult) -> Dict[str, Any]:
    value = result.value
    step = result.step
    base = {"name": step.name, "artifact_type": step.artifact_type}
    if isinstance(value, CustomObject):
        base.update(api_name=value.api_name, label=value.label)
    elif isinstance(value, CustomField):
        base.update(
            api_name=value.api_name, label=value.label, type=value.type,
            object_api_name=value.object_api_name,
        )
    elif isinstance(value, ApexClass):
        base.update(api_name=value.api_name, body=value.body)
    elif isinstance(value, Flow):
        base.update(api_name=value.api_name, element_count=len(value.elements))
    return base


def _org_credentials(org_alias: str) -> tuple[str, str]:
    try:
        org = get_org(org_alias or None)
    except SfCliError as exc:
        raise ValueError(str(exc)) from exc
    return org.instance_url, org.access_token


@mcp.tool()
async def build(request: str) -> Dict[str, Any]:
    """
    Turn a natural-language Salesforce request into a plan of typed steps
    (Custom Object / Custom Field / Apex Class / Flow), then generate each
    one. Nothing is sent to Salesforce - this only produces reviewable IR.

    Returns a build_id (pass it to approve/revise/validate/deploy), its
    version (pass this exact number to approve - it changes if the build is
    later revised), and a summary of every step generated. Nothing here is
    approved yet: call approve before validate or deploy will accept it.
    """
    provider = build_provider(None, None, "medium")
    plan_result = PlannerGenerator(provider).generate(request)
    plan: Plan = plan_result.value
    steps = execute_plan(provider, plan)

    build_id = uuid.uuid4().hex[:12]
    entry = Build(steps=steps)
    BUILDS[build_id] = entry
    return {
        "build_id": build_id,
        "version": entry.version,
        "steps": [_step_summary(r) for r in steps],
        "report_html": render_html_fragment(steps),
    }


@mcp.tool()
async def approve(build_id: str, version: int) -> Dict[str, Any]:
    """
    Approve a build so validate/deploy will accept it - the same gate the
    web UI's "Approve all" enforces before its own Validate/Deploy buttons.
    `version` must match the build's current version exactly (returned by
    build, or by revise if the build changed since); a mismatch means the
    build was revised since you last looked at it and needs reviewing again
    before it can be approved.
    """
    build_entry = BUILDS.get(build_id)
    if build_entry is None:
        raise ValueError(f"Unknown build_id {build_id!r} - call build first.")
    if version != build_entry.version:
        raise ValueError(
            f"This build is at version {build_entry.version}, not {version} - "
            "it changed since you last looked at it. Review it again before approving."
        )
    build_entry.approved_version = build_entry.version
    return {"build_id": build_id, "version": build_entry.version, "approved": True}


@mcp.tool()
async def revise(build_id: str, step_name: str, instruction: str) -> Dict[str, Any]:
    """
    Regenerate one step of an existing build with a change you ask for -
    e.g. "use a Picklist instead of a Checkbox for Active_Coverage_Status__c".
    Continues that step's own generation conversation
    (flowtool.planner.refine_step) rather than starting it over, and updates
    the build in place, so a later validate/deploy call on the same
    build_id picks up the revision.
    """
    build_entry = BUILDS.get(build_id)
    if build_entry is None:
        raise ValueError(f"Unknown build_id {build_id!r} - call build first.")
    index = next(
        (i for i, r in enumerate(build_entry.steps) if r.step.name == step_name), None
    )
    if index is None:
        names = [r.step.name for r in build_entry.steps]
        raise ValueError(f"No step named {step_name!r} in this build. Steps: {names}")

    provider = build_provider(None, None, "medium")
    updated = refine_step(provider, build_entry.steps[index], instruction)
    build_entry.steps[index] = updated
    # Not touching approved_version is what makes build_entry.approved false
    # again - same mechanism as PlanSession's repair/refine handling.
    build_entry.version += 1

    return {
        "step": _step_summary(updated),
        "version": build_entry.version,
        "report_html": render_html_fragment(build_entry.steps),
    }


@mcp.tool()
async def validate(build_id: str, org_alias: str) -> Dict[str, Any]:
    """
    Check-only dry run of a build against a real org - creates nothing,
    reports exactly what Salesforce would reject if deployed. Requires the
    build to be approved first (call approve). org_alias is an org the `sf`
    CLI is already authenticated against (`sf org login web --alias
    <alias>`); no token is passed through this call.
    """
    build_entry = BUILDS.get(build_id)
    if build_entry is None:
        raise ValueError(f"Unknown build_id {build_id!r} - call build first.")
    if not build_entry.approved:
        raise ValueError("Approve this build first (call approve) before validating it.")
    instance_url, token = _org_credentials(org_alias)
    files, types = _bundle_files_and_types(build_entry.steps)
    result = await validate_bundle(instance_url, token, files, types, check_only=True)
    return {"success": result.success, "status": result.status, "failures": _failures(result)}


@mcp.tool()
async def deploy(build_id: str, org_alias: str, confirm: bool) -> Dict[str, Any]:
    """
    Deploy a build to a real org for real - every step, in one transaction.
    This is NOT check-only: it creates/activates real metadata. Requires the
    build to be approved first (call approve), and confirm=true as an
    explicit, separate signal - the same two gates the web UI enforces
    before its own Deploy button. Call validate first too, though nothing
    here enforces that one.
    """
    if not confirm:
        raise ValueError("Deploying needs an explicit confirm=true.")
    build_entry = BUILDS.get(build_id)
    if build_entry is None:
        raise ValueError(f"Unknown build_id {build_id!r} - call build first.")
    if not build_entry.approved:
        raise ValueError("Approve this build first (call approve) before deploying it.")
    instance_url, token = _org_credentials(org_alias)
    files, types = _bundle_files_and_types(build_entry.steps)
    result = await validate_bundle(instance_url, token, files, types, check_only=False)

    setup_urls: Dict[str, str] = {}
    if result.success:
        for step_result in build_entry.steps:
            value = step_result.value
            if isinstance(value, CustomObject):
                url = await component_setup_url(instance_url, token, "object", value.api_name)
            elif isinstance(value, CustomField):
                url = await component_setup_url(
                    instance_url, token, "field", value.api_name,
                    object_api_name=value.object_api_name,
                )
            elif isinstance(value, ApexClass):
                url = await component_setup_url(instance_url, token, "apex", value.api_name)
            elif isinstance(value, Flow):
                url = await component_setup_url(instance_url, token, "flow", value.api_name)
            else:
                continue
            setup_urls[step_result.step.name] = url

    return {
        "success": result.success,
        "status": result.status,
        "failures": _failures(result),
        "setup_urls": setup_urls,
    }


if __name__ == "__main__":
    mcp.run()
