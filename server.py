"""
HTTP wrapper around the pipeline in forge.py.

The approval gate is enforced here, not in the browser. Every change to a flow
bumps its version, and validate and deploy refuse to run unless the version the
user approved is still the current one. A client-side check would be decoration.

    python server.py     ->  http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from flowtool import activity
from flowtool.config import load_env
from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass, ApexTrigger
from flowtool.ir_lwc import LightningComponent
from flowtool.ir_mdt import CustomMetadataRecord, MetadataType
from flowtool.ir_object import CustomField, CustomObject
from flowtool.ir_platform_event import PlatformEvent
from flowtool.ir_agent import Agent
from flowtool.ir_prompt_template import PromptTemplate
from flowtool.ir_permset import PermissionSetGrant, build_grant_from_steps, sanitize_api_name
from flowtool.llm import (
    AnthropicProvider,
    ApexClassGenerator,
    ApexTriggerGenerator,
    CustomFieldGenerator,
    CustomObjectGenerator,
    FlowGenerator,
    GenerationResult,
    IRGenerationResult,
    KB_CHAT_PROMPT,
    LLMError,
    LwcGenerator,
    Message,
    PromptTemplateGenerator,
    Provider,
    pair_planner,
    planner_of,
)
from flowtool.llm import GeminiProvider, OllamaProvider
from flowtool.lwc_guide import to_lwc_test_guide
from flowtool.mermaid import element_index, to_markdown, to_mermaid, to_test_guide
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.planner import (
    Plan, PlanStep, PlannerGenerator, StepResult, clean_house_rules, execute_plan, house_rules_text,
    refine_step, repair_step, revise_plan,
)
from flowtool.compare import OrgReader, compare_steps
from flowtool.object_import import (
    UnsupportedObject, list_custom_fields, list_custom_objects, parse_custom_field,
    parse_custom_object, retrieve_custom_field, retrieve_custom_object,
)
from flowtool.prompt_import import (
    UnsupportedTemplate, list_prompt_templates, parse_prompt_template, retrieve_prompt_template,
)
from flowtool.reference import clean_reference, tag_description, tag_value
from flowtool.report import render_standalone_report
from flowtool.scenarios import ScenarioGenerator, TestScenarios
from flowtool import anon_apex
from flowtool import github, hosted, privacy, ratelimit, savefile, share
from flowtool.sfdc import (
    ORG_SUMMARY_TYPE_GROUPS,
    ComponentProblem,
    MetadataClient,
    RetrieveError,
    _deploy_package_xml,
    _parse_lwc_meta,
    build_deploy_package,
    component_setup_url,
    list_apex_classes,
    list_apex_triggers,
    list_flows,
    list_lwc_components,
    list_permission_sets,
    retrieve_all_flows,
    retrieve_apex_class,
    retrieve_apex_trigger,
    retrieve_flow,
    retrieve_lwc_component,
    retrieve_org_summary_zip,
    retrieve_permission_set,
    validate_bundle,
)
from flowtool.xmlgen import generate as generate_xml
from flowtool.xmlgen_apex import generate_apex, generate_apex_trigger
from flowtool.xmlgen_lwc import generate_lwc
from flowtool.xmlgen_mdt import generate_mdt_record, generate_mdt_type
from flowtool.xmlgen_object import generate_field_delta, generate_object
from flowtool.xmlgen_platform_event import generate_platform_event
from flowtool.xmlgen_prompt_template import generate_prompt_template
from flowtool.xmlgen_agent import MissingAgentUser, generate_agent_files
from flowtool.prompt_activation import activate_templates
from flowtool.agent_publish import AgentPublishError, publish_employee_agent
from flowtool.xmlgen_permset import generate_permission_set, merge_permission_set_xml
from survey import Survey, text_report

ROOT = Path(__file__).parent
load_env(ROOT)

# Lives inside the flowtool package (rather than beside server.py) so it ships
# as package data when this repo is installed as a dependency elsewhere.
STATIC = ROOT / "flowtool" / "static"

app = FastAPI(title="SFDC Flow Tool", dependencies=[Depends(hosted.guard), Depends(ratelimit.require_own_key), Depends(ratelimit.guard)])

# Mermaid is vendored under /static/vendor rather than pulled from a CDN, so
# script-src needs nothing beyond 'self' - a future inline <script> added by
# accident will fail to run instead of quietly working. style-src allows
# inline: mermaid sets style="..." attributes directly on the SVG nodes it
# renders, and there is no static hash/nonce for content generated per
# diagram. Inline style is a much smaller foothold than inline script (no
# code execution), so this is a reasonable place to loosen the policy.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Tag everything this request kicks off with the browser's id, so the
    # activity feed (flowtool/activity.py) is served back only to its owner.
    activity.client_id.set(request.headers.get("x-client-id", "")[:64] or None)
    origin = request.headers.get("origin", "").rstrip("/")
    cors_ok = bool(origin) and origin in hosted.cors_origins()
    if cors_ok and request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        # Preflight from an allowed origin (the Chrome extension): answered here, never routed.
        response = Response(status_code=204)
    else:
        response = await call_next(request)
    if cors_ok:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = ", ".join(hosted.HEADERS_ALLOWED)
        response.headers["Access-Control-Max-Age"] = "600"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # StaticFiles sends an ETag but no Cache-Control, so browsers cache the
    # page and its scripts heuristically - after a deploy a user can be left
    # with new HTML driving an old app.js. no-cache still allows a cheap 304
    # revalidation; it just stops a stale copy being used without asking.
    kind = response.headers.get("content-type", "")
    if "cache-control" not in response.headers and kind.startswith(
        ("text/html", "text/css", "text/javascript", "application/javascript")
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


activity.install()


@app.get("/api/activity")
async def activity_feed(request: Request, after: int = 0) -> Dict[str, Any]:
    """This browser's log lines (model calls, repair attempts, ...) since `after`."""
    cid = request.headers.get("x-client-id", "")[:64]
    if not cid:
        return {"entries": [], "next": after}
    return activity.since(cid, after)


PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider, "ollama": OllamaProvider}

PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY2"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "ollama": ("OLLAMA_API_KEY",),
}


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


@dataclass
class PendingDeploy:
    """
    A validate/deploy running in the background. Heroku's router kills any
    single request after 30s, and a Salesforce deploy can occasionally run
    longer than that, so /start kicks the work off as a background task and
    /status polls it - the task itself is the same validate_bundle() call this
    used to just await directly.
    """

    task: "asyncio.Task"
    instance_url: str
    token: str


@dataclass
class PendingLLM:
    """
    A refine/repair running in the background, held on the session it will
    update once it lands. `note` is what session.record() logs to history -
    it has to be captured now, since the /status call has no other way to
    know what instruction started this.
    """

    task: "asyncio.Task"
    note: str


@dataclass
class Session:
    """
    Holds one artifact under review - a Flow (the original, larger use case), an
    Apex class, an Apex trigger, or a Lightning Web Component, opened from an
    org for editing or designed from scratch. `kind` picks which; `result` is
    a `GenerationResult` (`.flow`) for a Flow or an `IRGenerationResult`
    (`.value`) for anything else - the `flow`/`apex`/`trigger`/`lwc`
    properties below are what the rest of this module reads instead of
    poking at `.result` directly, so the shapes stay hidden behind one
    interface.
    """

    generator: Any
    result: Any
    kind: Literal["flow", "apex", "trigger", "lwc"] = "flow"
    activate: bool = False
    api_version: str = "64.0"
    # Bumped on every change; deploy compares the two.
    version: int = 1
    approved_version: int = 0
    history: List[Dict[str, str]] = field(default_factory=list)
    # What the org said last time, kept so a repair does not depend on the
    # browser sending error text back to the server.
    last_failures: List[str] = field(default_factory=list)
    # True when the artifact came out of the org rather than from a description.
    imported: bool = False
    # Optional external reference (e.g. a JIRA key) stamped onto what deploys
    # or exports - see flowtool/reference.py.
    reference: Optional[str] = None
    pending_deploy: Optional[PendingDeploy] = None
    pending_llm: Optional[PendingLLM] = None
    pending_explain: Optional["asyncio.Task"] = None

    @property
    def flow(self) -> Flow:
        return self.result.flow

    @property
    def apex(self) -> ApexClass:
        return self.result.value

    @property
    def trigger(self) -> ApexTrigger:
        return self.result.value

    @property
    def lwc(self) -> LightningComponent:
        return self.result.value

    @property
    def artifact(self) -> Any:
        if self.kind == "flow":
            return self.flow
        if self.kind == "apex":
            return self.apex
        if self.kind == "lwc":
            return self.lwc
        return self.trigger

    @property
    def artifact_name(self) -> str:
        return self.artifact.api_name

    @property
    def approved(self) -> bool:
        return self.approved_version == self.version

    def record(self, result, note: str) -> None:
        self.result = result
        self.version += 1
        self.history.append({"note": note, "version": str(self.version)})
        self.apply_policy()

    def apply_policy(self) -> None:
        """Status and API version are the tool's call, never the model's."""
        if self.kind != "flow":
            self.artifact.api_version = self.api_version
            return
        self.flow.status = "Active" if self.activate else "Draft"
        self.flow.api_version = self.api_version


SESSIONS: Dict[str, Session] = {}


@dataclass
class PendingImport:
    """A retrieve running in the background - no session exists yet to hang it
    on, so it gets its own id until the flow comes back and adopts one."""

    task: "asyncio.Task"
    instance_url: str
    request: "ImportRequest"


IMPORTS: Dict[str, PendingImport] = {}


@dataclass
class PendingDesign:
    """A generation running in the background - same reasoning as
    PendingImport, no session exists until the model comes back."""

    task: "asyncio.Task"
    generator: FlowGenerator
    request: str
    activate: bool
    api_version: str


DESIGN_JOBS: Dict[str, PendingDesign] = {}


@dataclass
class PendingLwcDesign:
    """PendingDesign's counterpart for the dedicated single-LWC create path -
    a separate dataclass (not reused) because its generator returns the
    generic IRGenerationResult, not Flow's own GenerationResult, and there is
    no `activate` flag to carry (an LWC has no Draft/Active concept)."""

    task: "asyncio.Task"
    generator: LwcGenerator
    request: str
    api_version: str


LWC_DESIGN_JOBS: Dict[str, PendingLwcDesign] = {}


@dataclass
class PendingSurvey:
    """A whole-org retrieve-and-score running in the background - same
    reasoning as PendingImport, just scoped to every flow instead of one."""

    task: "asyncio.Task"


SURVEY_JOBS: Dict[str, PendingSurvey] = {}


@dataclass
class PendingOrgSummaryZip:
    """A broad metadata retrieve running in the background - same reasoning
    as PendingSurvey, just a wider net of types for the org-summary
    knowledge base instead of flows alone."""

    task: "asyncio.Task"


ORG_SUMMARY_JOBS: Dict[str, PendingOrgSummaryZip] = {}


@dataclass
class PendingKbAnswer:
    """An org-summary question running in the background. Holds the provider
    too, not just the task - usage is read off it once the task completes,
    the same way a Session holds onto its generator for the same reason."""

    task: "asyncio.Task"
    provider: Provider


KB_JOBS: Dict[str, PendingKbAnswer] = {}


def waiting(provider: Provider) -> Dict[str, Any]:
    """
    A `{"done": False}` poll response, plus - when the provider is mid-retry
    on a rate limit or a transient server error - what it's waiting on. Only
    GeminiProvider currently sets `retry_status` (see flowtool/llm.py); any
    other provider simply has none, so this stays `None` for it rather than
    needing a per-provider special case here.
    """
    return {"done": False, "retry": getattr(provider, "retry_status", None)}


def llm_result(task: "asyncio.Task"):
    """
    Unwrap a background LLM task, turning any failure into an HTTPException
    instead of letting an exception type nobody anticipated reach Starlette's
    bare 500. That gap is exactly how a missing Anthropic key, surfaced by
    complete_text as a raw TypeError instead of an LLMError, once turned into
    a content-free "Internal Server Error" on Explain - complete_json caught
    it, complete_text didn't. This is the backstop for the next provider gap
    like it, whatever shape that turns out to be.
    """
    try:
        return task.result()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Unexpected error: {exc}") from exc


def get_session(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session. Start a new design.")
    return session


def view(session_id: str, session: Session) -> Dict[str, Any]:
    if session.kind in ("apex", "trigger"):
        artifact = session.artifact
        return {
            "session_id": session_id,
            "kind": session.kind,
            "version": session.version,
            "approved": session.approved,
            "api_name": artifact.api_name,
            "label": artifact.api_name,
            "description": artifact.description,
            "status": artifact.status,
            "api_version": artifact.api_version,
            "reference": session.reference,
            "ir": artifact.model_dump(exclude_none=True),
            "repairs": session.result.repairs,
            "usage": session.generator.provider.usage.as_dict(),
            "imported": session.imported,
            "history": session.history,
        }

    if session.kind == "lwc":
        component = session.lwc
        return {
            "session_id": session_id,
            "kind": "lwc",
            "version": session.version,
            "approved": session.approved,
            "api_name": component.api_name,
            "label": component.api_name,
            "description": component.description,
            "api_version": component.api_version,
            "is_exposed": component.is_exposed,
            "targets": component.targets,
            "has_css": component.css is not None,
            "reference": session.reference,
            "test_guide": to_lwc_test_guide(component),
            "ir": component.model_dump(exclude_none=True),
            "repairs": session.result.repairs,
            "usage": session.generator.provider.usage.as_dict(),
            "imported": session.imported,
            "history": session.history,
        }

    flow = session.flow
    return {
        "session_id": session_id,
        "kind": "flow",
        "version": session.version,
        "approved": session.approved,
        "api_name": flow.api_name,
        "label": flow.label,
        "description": flow.description,
        "status": flow.status,
        "api_version": flow.api_version,
        "element_count": len(flow.elements),
        "trigger": (
            "Screen flow (run by a user)"
            if flow.process_type == "Flow"
            else f"{flow.start.object} / {flow.start.record_trigger_type} "
            f"/ {flow.start.trigger_type}"
            if flow.start.object
            else "Autolaunched"
        ),
        "mermaid": to_mermaid(flow),
        "markdown": to_markdown(flow),
        "test_guide": to_test_guide(flow),
        "element_index": element_index(flow),
        "reference": session.reference,
        "ir": flow.model_dump(exclude_none=True),
        "repairs": session.result.repairs,
        "usage": session.generator.provider.usage.as_dict(),
        "imported": session.imported,
        "history": session.history,
    }


# --------------------------------------------------------------------------
# Providers and orgs
# --------------------------------------------------------------------------


def available_providers() -> List[str]:
    return [
        name
        for name, keys in PROVIDER_KEYS.items()
        if any(os.environ.get(key) for key in keys)
    ]


def default_provider(found: List[str]) -> Optional[str]:
    """
    The provider the Options panel opens on: DEFAULT_TOKEN_PROVIDER (gemini
    unless set) when it has a key, otherwise whichever provider does.
    """
    preferred = os.environ.get("DEFAULT_TOKEN_PROVIDER", "").strip().lower() or "gemini"
    if preferred in found:
        return preferred
    return found[0] if found else None


def build_provider(
    name: Optional[str], model: Optional[str], effort: str, api_key: Optional[str] = None
) -> Provider:
    if not name:
        found = available_providers()
        if not found:
            raise LLMError(
                "No LLM key found. Either paste one in the UI's Options panel, or "
                f"put GEMINI_API_KEY, ANTHROPIC_API_KEY, or OLLAMA_API_KEY in a "
                f"{ROOT / '.env'} file and restart the server."
            )
        name = default_provider(found)
    if name not in PROVIDERS:
        raise LLMError(f"Unknown provider {name!r}.")
    options: Dict[str, Any] = {"effort": effort}
    if model:
        options["model"] = model
    if api_key:
        options["api_key"] = api_key
    return PROVIDERS[name](**options)


# The models a plan's steps are built with, matched against what the key can
# use (newest first): `complex` for the step types that need judgement, `simple`
# for the mechanical rest. Ollama has no such tiers.
_WORKER_TIERS = {
    "anthropic": (lambda name: "sonnet" in name, lambda name: "haiku" in name),
    "gemini": (
        lambda name: "pro" in name,
        lambda name: "flash" in name and "lite" not in name,
    ),
}
_COMPLEX_STEP_TYPES = ("apex", "flow", "lwc")


def pick_worker_models(planner: Provider) -> tuple[str, str]:
    """(complex, simple) model names for `planner`'s provider."""
    tiers = _WORKER_TIERS.get(planner.name)
    if tiers is None:
        raise LLMError(f"Auto-select for workers isn't available for {planner.name}.")
    available = planner.list_models()
    picked = []
    for matches in tiers:
        found = next((name for name in available if matches(name)), None)
        if found is None:
            raise LLMError(f"No suitable model for workers was found for this {planner.name} key.")
        picked.append(found)
    return picked[0], picked[1]


def with_auto_workers(planner: Provider, effort: str, api_key: Optional[str]) -> Provider:
    """
    `planner` plans; the returned provider builds Apex, Flow and LWC steps with
    the provider's mid-tier model and everything else with its cheapest, both
    at medium effort.
    """
    complex_model, simple_model = pick_worker_models(planner)

    def worker(model: str) -> Provider:
        if model == planner.model:
            return planner
        return build_provider(planner.name, model, "medium", api_key)

    simple = worker(simple_model)
    complex_ = simple if complex_model == simple_model else worker(complex_model)
    logging.getLogger("flowtool").info(
        "Planning with %s, building with %s (Apex/Flow/LWC: %s)",
        planner.model, simple_model, complex_model,
    )
    return pair_planner(simple, planner, {t: complex_ for t in _COMPLEX_STEP_TYPES})


def credentials(
    org_alias: Optional[str],
    instance_url: Optional[str] = None,
    access_token: Optional[str] = None,
) -> tuple[str, str]:
    """
    A token the browser already has (from logging into Salesforce itself, via
    the OAuth implicit flow) wins - it never touches this server's disk or
    config either way. Otherwise fall back to the sf CLI, the only path when
    the browser did not send its own credentials.
    """
    if hosted.is_hosted():
        # Never reached by a real route (hosted.guard rejects first); a second lock so a
        # new endpoint that forgets to be listed still cannot use a token or the sf CLI.
        raise HTTPException(403, hosted.MESSAGE)
    if instance_url and access_token:
        return instance_url, access_token

    from flowtool.orgs import SfCliError, get_org

    try:
        org = get_org(org_alias or None)
    except SfCliError as exc:
        raise HTTPException(400, str(exc)) from exc
    return org.instance_url, org.access_token


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class DesignRequest(BaseModel):
    request: str
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    activate: bool = False
    api_version: str = "64.0"
    api_key: Optional[str] = None


class LwcDesignRequest(BaseModel):
    """Same shape as DesignRequest, minus `activate` - an LWC has no Draft/
    Active concept the way a Flow does."""

    request: str
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "64.0"
    api_key: Optional[str] = None


class RefineRequest(BaseModel):
    session_id: str
    instruction: str


class ApproveRequest(BaseModel):
    session_id: str
    version: int


class OrgRequest(BaseModel):
    session_id: str
    org: Optional[str] = None
    # Set when the browser logged into Salesforce itself (OAuth implicit
    # flow); takes over from `org` when present. See credentials().
    instance_url: Optional[str] = None
    access_token: Optional[str] = None


class FlowsRequest(BaseModel):
    """No session_id - the flow picker is asked for before any session
    exists. POST rather than GET so access_token travels in the body, not a
    URL that ends up in access logs."""

    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None


class ApexClassesRequest(FlowsRequest):
    """Same shape and same reasoning as FlowsRequest - the Apex picker asked
    for before any session exists."""


class ApexTriggersRequest(FlowsRequest):
    """Same shape again - the Apex trigger picker."""


class LwcComponentsRequest(FlowsRequest):
    """Same shape again - the Lightning Web Component picker."""


class PermissionSetsRequest(FlowsRequest):
    """Same shape again - the Permission Set picker (the "add to an
    existing one" case, asked for before any plan session exists)."""


class DeployRequest(OrgRequest):
    confirm: bool = False


class SurveyRequest(BaseModel):
    """No session_id - a survey scores the whole org, run before any flow
    has been designed or opened."""

    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None


class OrgSummaryRequest(SurveyRequest):
    # Which checkbox groups from ORG_SUMMARY_TYPE_GROUPS to retrieve. None
    # means whatever that list's own defaults are - the browser always sends
    # this explicitly once the dialog's checkboxes have rendered, so None in
    # practice only covers a request that beat the config fetch there.
    groups: Optional[List[str]] = None


class ImportRequest(BaseModel):
    api_name: str
    # "custom_object" opens as a plan too (the object, then a step per field); "custom_field" is "Object.Field__c".
    # "prompt_template" opens as a one-step plan (Build multiple things), which already
    # reviews, revises, validates and deploys that type; the others open as a session.
    kind: Literal["flow", "apex", "trigger", "lwc", "prompt_template", "custom_object", "custom_field"] = "flow"
    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "64.0"
    api_key: Optional[str] = None


class ExplainRequest(BaseModel):
    session_id: str
    question: Optional[str] = None


class KbChatRequest(BaseModel):
    """
    No session_id: the org-summary knowledge base isn't tied to a flow, and
    isn't kept server-side between questions either - it travels with each
    one, the same stateless shape as FlowGenerator.explain().
    """

    markdown: str
    question: str
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_key: Optional[str] = None


class ModelsRequest(BaseModel):
    provider: Optional[str] = None
    # POSTed rather than in a query string: a key does not belong in a URL,
    # which is the one part of a request that gets logged everywhere.
    api_key: Optional[str] = None


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/api/config")
def config() -> Dict[str, Any]:
    providers = available_providers()
    if hosted.is_hosted():
        # No org of any kind here: no Connected App id, no sf CLI aliases, no server paths.
        return {
            "hosted": True,
            "providers": providers,
            "all_providers": list(PROVIDERS),
            "default_provider": default_provider(providers),
            "heroku": False,
            "org_summary_type_groups": [
                {"group": g["group"], "label": g["label"], "default": g["default"]}
                for g in ORG_SUMMARY_TYPE_GROUPS
            ],
            "show_ir_subtab": False,
            "show_auto_workers": os.environ.get("SHOW_AUTO_WORKERS", "").strip().lower() == "true",
            "highlight_plan_mode": os.environ.get("HIGHLIGHT_PLAN_MODE", "").strip().lower() == "true",
            "share_enabled": share.configured(),
        }
    orgs: List[str] = []
    cli = True
    try:
        from flowtool.orgs import list_orgs

        orgs = list_orgs()
    except Exception:
        cli = False
    return {
        "providers": providers,
        "all_providers": list(PROVIDERS),
        "default_provider": default_provider(providers),
        "orgs": orgs,
        "sf_cli": cli,
        "env_file": str(ROOT / ".env"),
        # DYNO is set by the Heroku runtime on every dyno, unset everywhere
        # else - there is no .env file to point someone at on Heroku, since
        # config there lives in Config Vars and ROOT is wherever pip installed
        # this package, not the app's own directory.
        "heroku": "DYNO" in os.environ,
        # Same Connected App and same env var name as salesforce-debugtool, so
        # a Heroku deployment that already sets one for that app needs nothing
        # new here. Empty means the login buttons stay hidden.
        "clientId": os.environ.get("SF_CLIENT_ID", ""),
        # The org-summary checkbox list: group key, label, and whether it
        # starts checked. The browser never hard-codes this itself.
        "org_summary_type_groups": [
            {"group": g["group"], "label": g["label"], "default": g["default"]}
            for g in ORG_SUMMARY_TYPE_GROUPS
        ],
        # A raw IR dump is a debugging aid, not something most users need a
        # tab for - off unless a Heroku Config Var (or local env var) turns
        # it on, so it stays out of the way for everyone but this repo's own
        # maintainer(s).
        "show_ir_subtab": os.environ.get("SHOW_IR_SUBTAB", "").strip().lower() == "true",
        "show_auto_workers": os.environ.get("SHOW_AUTO_WORKERS", "").strip().lower() == "true",
        # Reorders "Build multiple things" to the front of the mode buttons
        # and visually highlights it - off by default so this stays a staged
        # rollout, gated the same way SHOW_IR_SUBTAB is, rather than a
        # behavior change that ships live to everyone at once.
        "highlight_plan_mode": os.environ.get("HIGHLIGHT_PLAN_MODE", "").strip().lower() == "true",
        # "Share this" needs Upstash Redis config vars - off (button hidden)
        # rather than a 400 on click when they aren't set.
        "share_enabled": share.configured(),
    }


@app.post("/api/models")
def models(body: ModelsRequest) -> Dict[str, Any]:
    """
    What this key can actually use, asked of the provider rather than hard-coded.

    A baked-in list goes stale silently: a model is retired and the only sign is
    a failure several seconds into a design. This also matters when a daily quota
    runs out on one model and the work can continue on another.
    """
    try:
        provider = build_provider(body.provider, None, "medium", body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    lister = getattr(provider, "list_models", None)
    if lister is None:
        return {"models": [], "default": getattr(provider, "model", None)}
    try:
        found = lister()
    except Exception as exc:
        # Usually a bad or missing key. Say so; do not echo the key back.
        raise HTTPException(400, f"Could not list models: {exc}") from exc
    return {"models": found, "default": getattr(provider, "model", None)}


@app.post("/api/design/start")
async def design_start(body: DesignRequest) -> Dict[str, Any]:
    """
    Kick off generation in the background and hand back a job id. The model
    call itself is synchronous (the provider SDKs are), so it runs in a
    thread rather than blocking the event loop - a slow or schema-fallback
    generation has run past 30s in practice, which is Heroku's request limit.
    """
    if not body.request.strip():
        raise HTTPException(400, "Describe what the flow should do.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    generator = FlowGenerator(provider)
    task = asyncio.create_task(asyncio.to_thread(generator.generate, body.request))
    job_id = uuid.uuid4().hex
    DESIGN_JOBS[job_id] = PendingDesign(
        task=task,
        generator=generator,
        request=body.request,
        activate=body.activate,
        api_version=body.api_version,
    )
    return {"job_id": job_id}


@app.get("/api/design/status")
async def design_status(job_id: str) -> Dict[str, Any]:
    pending = DESIGN_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown design job.")
    if not pending.task.done():
        return waiting(pending.generator.provider)
    del DESIGN_JOBS[job_id]

    result = llm_result(pending.task)

    session_id = uuid.uuid4().hex
    session = Session(
        generator=pending.generator,
        result=result,
        activate=pending.activate,
        api_version=pending.api_version,
        history=[{"note": pending.request, "version": "1"}],
    )
    session.apply_policy()
    SESSIONS[session_id] = session
    return {"done": True, **view(session_id, session)}


@app.post("/api/lwc/start")
async def lwc_start(body: LwcDesignRequest) -> Dict[str, Any]:
    """Design a new Lightning Web Component from a description - the LWC
    counterpart to /api/design/start, same background-job-and-poll shape."""
    if not body.request.strip():
        raise HTTPException(400, "Describe what the component should do.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    generator = LwcGenerator(provider)
    task = asyncio.create_task(asyncio.to_thread(generator.generate, body.request))
    job_id = uuid.uuid4().hex
    LWC_DESIGN_JOBS[job_id] = PendingLwcDesign(
        task=task, generator=generator, request=body.request, api_version=body.api_version,
    )
    return {"job_id": job_id}


@app.get("/api/lwc/status")
async def lwc_status(job_id: str) -> Dict[str, Any]:
    pending = LWC_DESIGN_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown lwc design job.")
    if not pending.task.done():
        return waiting(pending.generator.provider)
    del LWC_DESIGN_JOBS[job_id]

    result = llm_result(pending.task)

    session_id = uuid.uuid4().hex
    session = Session(
        generator=pending.generator,
        result=result,
        kind="lwc",
        api_version=pending.api_version,
        history=[{"note": pending.request, "version": "1"}],
    )
    session.apply_policy()
    SESSIONS[session_id] = session
    return {"done": True, **view(session_id, session)}


@app.post("/api/flows")
async def flows(body: FlowsRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        found = await list_flows(url, token)
    except RetrieveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "flows": [
            {
                "api_name": flow.api_name,
                "label": flow.label,
                "active": flow.active,
                "description": flow.description,
                "last_modified": flow.last_modified,
            }
            for flow in found
        ]
    }


@app.post("/api/apex-classes")
async def apex_classes(body: ApexClassesRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        found = await list_apex_classes(url, token)
    except RetrieveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "classes": [
            {"api_name": cls.api_name, "last_modified": cls.last_modified}
            for cls in found
        ]
    }


@app.post("/api/prompt-templates")
async def prompt_templates(body: ApexClassesRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        async with MetadataClient(url, token, "64.0") as client:
            found = await list_prompt_templates(client)
    except Exception as exc:
        raise HTTPException(400, f"Could not list prompt templates: {exc}") from exc
    return {"templates": found}


@app.post("/api/custom-objects")
async def custom_objects(body: ApexClassesRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        async with MetadataClient(url, token, "64.0") as client:
            found = await list_custom_objects(client)
    except Exception as exc:
        raise HTTPException(400, f"Could not list custom objects: {exc}") from exc
    return {"objects": found}


@app.post("/api/custom-fields")
async def custom_fields(body: ApexClassesRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        async with MetadataClient(url, token, "64.0") as client:
            found = await list_custom_fields(client)
    except Exception as exc:
        raise HTTPException(400, f"Could not list custom fields: {exc}") from exc
    return {"fields": found}


@app.post("/api/apex-triggers")
async def apex_triggers(body: ApexTriggersRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        found = await list_apex_triggers(url, token)
    except RetrieveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "triggers": [
            {"api_name": trig.api_name, "last_modified": trig.last_modified}
            for trig in found
        ]
    }


@app.post("/api/lwc-components")
async def lwc_components(body: LwcComponentsRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        found = await list_lwc_components(url, token)
    except RetrieveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "components": [
            {"api_name": comp.api_name, "last_modified": comp.last_modified}
            for comp in found
        ]
    }


@app.post("/api/permission-sets")
async def permission_sets(body: PermissionSetsRequest) -> Dict[str, Any]:
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        found = await list_permission_sets(url, token)
    except RetrieveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "permission_sets": [
            {"api_name": ps.api_name, "label": ps.label, "last_modified": ps.last_modified}
            for ps in found
        ]
    }


async def _run_survey(url: str, token: str) -> str:
    flows = await retrieve_all_flows(url, token)
    survey = Survey()
    for name, xml in flows.items():
        survey.add(name, xml)
    return text_report(survey)


@app.post("/api/survey/start")
async def survey_start(body: SurveyRequest) -> Dict[str, Any]:
    """
    Retrieve every flow in the org and score how many this build can already
    represent - same measurement as `python survey.py`, minus anything that
    would identify the org's own flows or data, since the result is meant to
    be copied out and shared as a support report.
    """
    url, token = credentials(body.org, body.instance_url, body.access_token)
    task = asyncio.create_task(_run_survey(url, token))
    job_id = uuid.uuid4().hex
    SURVEY_JOBS[job_id] = PendingSurvey(task=task)
    return {"job_id": job_id}


@app.get("/api/survey/status")
async def survey_status(job_id: str) -> Dict[str, Any]:
    pending = SURVEY_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown survey job.")
    if not pending.task.done():
        return {"done": False}
    del SURVEY_JOBS[job_id]
    try:
        text = pending.task.result()
    except (RetrieveError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        # Same reasoning as llm_result(): a network failure reaching the org
        # (DNS, TLS, a dropped connection) is not a type this endpoint
        # anticipated by name, and should not fall through to a bare 500.
        raise HTTPException(502, f"Could not reach the org: {exc}") from exc
    return {"done": True, "report": text}


@app.post("/api/org-summary/start")
async def org_summary_start(body: OrgSummaryRequest) -> Dict[str, Any]:
    """
    Retrieve the checkbox groups the browser asked for (ORG_SUMMARY_TYPE_GROUPS's
    own defaults if it asked for none) as a zip. The browser turns it into a
    Markdown knowledge base itself, with the same worker /metadata-kb uses on
    a file the user found and uploaded by hand - this just hands it a zip the
    server pulled directly instead.
    """
    url, token = credentials(body.org, body.instance_url, body.access_token)
    task = asyncio.create_task(retrieve_org_summary_zip(url, token, groups=body.groups))
    job_id = uuid.uuid4().hex
    ORG_SUMMARY_JOBS[job_id] = PendingOrgSummaryZip(task=task)
    return {"job_id": job_id}


@app.get("/api/org-summary/status")
async def org_summary_status(job_id: str) -> Dict[str, Any]:
    pending = ORG_SUMMARY_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown org summary job.")
    if not pending.task.done():
        return {"done": False}
    del ORG_SUMMARY_JOBS[job_id]
    try:
        zip_bytes = pending.task.result()
    except (RetrieveError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        # Same reasoning as survey_status(): a network failure reaching the
        # org is not a type this endpoint anticipated by name.
        raise HTTPException(502, f"Could not reach the org: {exc}") from exc
    return {"done": True, "zip_base64": base64.b64encode(zip_bytes).decode("ascii")}


@app.post("/api/kb-chat/start")
async def kb_chat_start(body: KbChatRequest) -> Dict[str, Any]:
    if not body.question.strip():
        raise HTTPException(400, "Ask something.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    task = asyncio.create_task(asyncio.to_thread(
        provider.complete_text,
        KB_CHAT_PROMPT,
        [Message(
            role="user",
            content=f"{body.question}\n\nOrg knowledge base:\n{body.markdown}",
        )],
    ))
    job_id = uuid.uuid4().hex
    KB_JOBS[job_id] = PendingKbAnswer(task=task, provider=provider)
    return {"job_id": job_id}


@app.get("/api/kb-chat/status")
async def kb_chat_status(job_id: str) -> Dict[str, Any]:
    pending = KB_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown kb-chat job.")
    if not pending.task.done():
        return waiting(pending.provider)
    del KB_JOBS[job_id]
    answer = llm_result(pending.task)
    return {"done": True, "answer": answer, "usage": pending.provider.usage.as_dict()}


@app.post("/api/import/start")
async def import_start(body: ImportRequest) -> Dict[str, Any]:
    """
    Kick off a retrieve in the background and hand back a job id. A retrieve
    can run long enough to cross Heroku's 30s request limit, so the browser
    polls /api/import/status instead of waiting on one request.
    """
    url, token = credentials(body.org, body.instance_url, body.access_token)
    if body.kind == "apex":
        task = asyncio.create_task(
            retrieve_apex_class(url, token, body.api_name, api_version=body.api_version)
        )
    elif body.kind == "trigger":
        task = asyncio.create_task(
            retrieve_apex_trigger(url, token, body.api_name, api_version=body.api_version)
        )
    elif body.kind == "lwc":
        task = asyncio.create_task(
            retrieve_lwc_component(url, token, body.api_name, api_version=body.api_version)
        )
    elif body.kind == "prompt_template":
        task = asyncio.create_task(_retrieve_prompt_template(url, token, body.api_name))
    elif body.kind == "custom_object":
        task = asyncio.create_task(_retrieve_custom_object(url, token, body.api_name))
    elif body.kind == "custom_field":
        task = asyncio.create_task(_retrieve_custom_field(url, token, body.api_name))
    else:
        task = asyncio.create_task(
            retrieve_flow(url, token, body.api_name, api_version=body.api_version)
        )
    job_id = uuid.uuid4().hex
    IMPORTS[job_id] = PendingImport(task=task, instance_url=url, request=body)
    return {"job_id": job_id}


async def _retrieve_prompt_template(url: str, token: str, api_name: str):
    async with MetadataClient(url, token, "64.0") as client:
        return await retrieve_prompt_template(client, api_name)


# An object with more custom fields than this asks which ones to bring into the plan.
FIELD_PICK_THRESHOLD = 12
OBJECT_PICKS: Dict[str, Any] = {}


class ObjectFieldsRequest(BaseModel):
    pick_id: str
    fields: List[str]


def _object_plan(body: ImportRequest, provider, instance_url: str, opened, chosen: List[str]) -> Dict[str, Any]:
    """A plan of the opened object and the chosen fields (all of them when `chosen` is every one)."""
    origin = f"from {instance_url}"
    steps: List[StepResult] = []
    obj_generator = CustomObjectGenerator(provider)
    obj_step = PlanStep(
        artifact_type="object", name=opened.object.api_name,
        brief=f"The custom object {opened.object.api_name}, opened from the org.",
    )
    steps.append(StepResult(
        step=obj_step, value=opened.object, repairs=0,
        messages=obj_generator.adopt(opened.object, f"the custom object {opened.object.api_name} {origin}").messages,
    ))
    field_generator = CustomFieldGenerator(provider)
    wanted = [f for f in opened.fields if f.api_name in set(chosen)]
    for fld in wanted:
        fld_step = PlanStep(
            artifact_type="field", name=f"{fld.object_api_name}.{fld.api_name}",
            brief=f"The custom field {fld.object_api_name}.{fld.api_name}, opened from the org.",
            depends_on=[obj_step.name],
        )
        steps.append(StepResult(
            step=fld_step, value=fld, repairs=0,
            messages=field_generator.adopt(fld, f"the custom field {fld.object_api_name}.{fld.api_name} {origin}").messages,
        ))
    notes = list(opened.notes)
    if opened.skipped:
        notes.append(
            "Fields left out of this plan because the tool cannot represent them (deploying leaves "
            "them as they are in the org): " + " ".join(opened.skipped)
        )
    left = len(opened.fields) - len(wanted)
    if left:
        notes.append(f"{left} other custom field(s) were not brought into this plan; deploying leaves them as they are.")
    plan_session = PlanSession(
        provider=provider, plan=Plan(steps=[s.step for s in steps], reasoning=" ".join(notes) or None),
        steps=steps, api_version=body.api_version,
    )
    plan_session.apply_policy()
    session_id = uuid.uuid4().hex
    PLAN_SESSIONS[session_id] = plan_session
    return {"done": True, "opened_as": "plan", **plan_view(session_id, plan_session)}


@app.post("/api/import/object-fields")
async def import_object_fields(body: ObjectFieldsRequest) -> Dict[str, Any]:
    """Second step of opening a large object: the fields the person ticked."""
    pick = OBJECT_PICKS.pop(body.pick_id, None)
    if pick is None:
        raise HTTPException(404, "That object is no longer open; open it again.")
    request, instance_url, opened = pick
    try:
        provider = build_provider(request.provider, request.model, request.effort, request.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _object_plan(request, provider, instance_url, opened, body.fields)


async def _retrieve_custom_object(url: str, token: str, api_name: str):
    async with MetadataClient(url, token, "64.0") as client:
        return await retrieve_custom_object(client, api_name)


async def _retrieve_custom_field(url: str, token: str, api_name: str):
    async with MetadataClient(url, token, "64.0") as client:
        return await retrieve_custom_field(client, api_name)


# kind -> (IR model, generator) for the two source-only artifact types, whose
# import handling is otherwise identical - Apex vs. Flow (a real graph to
# parse) is the shape difference that keeps them from sharing this branch too.
_APEX_LIKE = {
    "apex": (ApexClass, ApexClassGenerator, "Apex class"),
    "trigger": (ApexTrigger, ApexTriggerGenerator, "Apex trigger"),
}


@app.get("/api/import/status")
async def import_status(job_id: str) -> Dict[str, Any]:
    """
    Pull a Flow, Apex class or Apex trigger out of the org and adopt it, so
    the next refinement edits it rather than designing a replacement from its
    description.
    """
    pending = IMPORTS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown import job.")
    if not pending.task.done():
        return {"done": False}
    del IMPORTS[job_id]

    try:
        source = pending.task.result()
    except (RetrieveError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc

    body = pending.request
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _adopt_import(body, provider, source, pending.instance_url)


def _adopt_import(
    body: ImportRequest, provider: Provider, source: Any, origin: str,
    chosen: Optional[List[str]] = None, stateless_pick: bool = False,
) -> Dict[str, Any]:
    """
    The half of an import that needs no org: turn what was retrieved (`source`:
    flow XML, Apex body, a LightningComponent, a PromptTemplate, an OpenedObject
    or a CustomField) into a session, and return its view. `origin` is only the
    words "from ..." in the model's context (an instance URL when this server
    retrieved it, "the org" when the extension did).
    """
    session_id = uuid.uuid4().hex

    if body.kind == "prompt_template":
        template = source
        adopted = PromptTemplateGenerator(provider).adopt(
            template, f"the prompt template {body.api_name} from {origin}"
        )
        step = PlanStep(
            artifact_type="prompt_template", name=body.api_name,
            brief=f"The prompt template {body.api_name}, opened from the org.",
        )
        plan_session = PlanSession(
            provider=provider, plan=Plan(steps=[step]),
            steps=[StepResult(step=step, value=template, repairs=0, messages=adopted.messages)],
            api_version=body.api_version,
        )
        plan_session.apply_policy()
        PLAN_SESSIONS[session_id] = plan_session
        return {"done": True, "opened_as": "plan", **plan_view(session_id, plan_session)}

    if body.kind == "custom_object":
        opened = source
        if chosen is None and len(opened.fields) > FIELD_PICK_THRESHOLD:
            # Too many for one plan to be readable: ask which fields to bring in.
            pick_id = uuid.uuid4().hex
            if stateless_pick:
                pick_id = None  # nothing kept here: the caller sends `fields` back with the XML
            else:
                OBJECT_PICKS[pick_id] = (body, origin, opened)
            return {
                "done": True, "opened_as": "choose_fields", "pick_id": pick_id,
                "object": opened.object.api_name,
                "fields": [{"api_name": f.api_name, "label": f.label, "type": f.type} for f in opened.fields],
                "skipped": opened.skipped, "notes": opened.notes,
            }
        return _object_plan(
            body, provider, origin, opened,
            chosen if chosen is not None else [f.api_name for f in opened.fields],
        )

    if body.kind == "custom_field":
        fld = source
        fld_step = PlanStep(
            artifact_type="field", name=body.api_name,
            brief=f"The custom field {body.api_name}, opened from the org.",
        )
        reasoning = None
        if fld.type == "Picklist":
            reasoning = (
                "If this object has record types, picklist values are enabled per record type: a value "
                "added here is not offered on a record type until it is added there."
            )
        step_result = StepResult(
            step=fld_step, value=fld, repairs=0,
            messages=CustomFieldGenerator(provider).adopt(
                fld, f"the custom field {body.api_name} from {origin}").messages,
        )
        plan_session = PlanSession(
            provider=provider, plan=Plan(steps=[fld_step], reasoning=reasoning),
            steps=[step_result], api_version=body.api_version,
        )
        plan_session.apply_policy()
        PLAN_SESSIONS[session_id] = plan_session
        return {"done": True, "opened_as": "plan", **plan_view(session_id, plan_session)}

    if body.kind in _APEX_LIKE:
        model_cls, generator_cls, noun = _APEX_LIKE[body.kind]
        artifact = model_cls(
            api_name=body.api_name, body=source, api_version=body.api_version,
        )
        generator = generator_cls(provider)
        session = Session(
            generator=generator,
            result=generator.adopt(
                artifact, f"the {noun} {body.api_name} from {origin}"
            ),
            kind=body.kind,
            api_version=body.api_version,
            history=[{"note": f"Imported {body.api_name} from the org", "version": "1"}],
            imported=True,
        )
    elif body.kind == "lwc":
        # retrieve_lwc_component already returns the fully-parsed
        # LightningComponent (js/html/css/meta unpacked from the bundle),
        # unlike the _APEX_LIKE branch above which builds the IR from a raw
        # source string - a bundle has no single "body" to hand a generic
        # model_cls(...) constructor.
        component: LightningComponent = source
        generator = LwcGenerator(provider)
        session = Session(
            generator=generator,
            result=generator.adopt(
                component, f"the Lightning Web Component {body.api_name} from {origin}"
            ),
            kind="lwc",
            api_version=component.api_version,
            history=[{"note": f"Imported {body.api_name} from the org", "version": "1"}],
            imported=True,
        )
    else:
        try:
            flow = parse_flow(source, api_name=body.api_name)
        except UnsupportedFlow as exc:
            # Refusing is the point: a diagram missing the parts we cannot
            # model would describe a different flow than the one in the org.
            raise HTTPException(422, str(exc)) from exc

        generator = FlowGenerator(provider)
        session = Session(
            generator=generator,
            result=generator.adopt(
                flow, f"the flow {body.api_name} from {origin}"
            ),
            kind="flow",
            # An imported flow keeps the status it already has in the org, so
            # opening one to read it cannot quietly propose deactivating it.
            activate=flow.status == "Active",
            api_version=flow.api_version,
            history=[{"note": f"Imported {body.api_name} from the org", "version": "1"}],
            imported=True,
        )

    session.apply_policy()
    SESSIONS[session_id] = session
    return {"done": True, **view(session_id, session)}


class ImportParseRequest(BaseModel):
    """
    The post-retrieve half of /api/import/start: the extension retrieved the
    metadata from the org itself and sends what it got. `xml` carries the
    single-file kinds (flow, prompt_template, custom_object/custom_field: the
    owning object's XML), or the source text for apex/trigger; `files` carries
    an LWC bundle, keyed by file name (x.js, x.html, x.css, x.js-meta.xml).
    """

    api_name: str
    kind: Literal["flow", "apex", "trigger", "lwc", "prompt_template", "custom_object", "custom_field"] = "flow"
    xml: Optional[str] = None
    files: Optional[Dict[str, str]] = None
    # custom_object with many fields: the ones to bring in (answer to a `choose_fields` reply).
    fields: Optional[List[str]] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "64.0"
    api_key: Optional[str] = None


def _lwc_from_files(api_name: str, files: Dict[str, str], api_version: str) -> LightningComponent:
    def pick(suffix: str) -> Optional[str]:
        for name, text in files.items():
            if name.rsplit("/", 1)[-1].lower().endswith(suffix):
                return text
        return None

    js, html = pick(".js"), pick(".html")
    if js is None or html is None:
        raise HTTPException(400, "An LWC needs at least its .js and .html files in `files`.")
    meta = pick(".js-meta.xml") or ""
    try:
        is_exposed, targets, retrieved_version = _parse_lwc_meta(meta)
    except Exception as exc:  # noqa: BLE001 - malformed sidecar XML
        raise HTTPException(422, f"The .js-meta.xml could not be read: {exc}") from exc
    return LightningComponent(
        api_name=api_name, js=js, html=html, css=pick(".css"),
        is_exposed=is_exposed, targets=targets, api_version=retrieved_version or api_version,
    )


def _xml_from_zip_files(kind: str, api_name: str, files: Dict[str, str]) -> Optional[str]:
    """The one member a retrieved zip holds for this kind (`files` is path -> text)."""
    suffix = {"flow": ".flow", "prompt_template": ".genAiPromptTemplate"}.get(kind, ".object")
    stem = api_name.partition(".")[0] if kind == "custom_field" else api_name
    matches = {p: t for p, t in files.items() if p.endswith(suffix)}
    for path, text in matches.items():
        if path.rsplit("/", 1)[-1] == stem + suffix:
            return text
    return next(iter(matches.values()), None)


@app.post("/api/import/parse")
async def import_parse(body: ImportParseRequest) -> Dict[str, Any]:
    """
    Open metadata the caller already retrieved: same result as /api/import/status
    (a session view, or a plan view for the plan kinds) without any org contact,
    so nothing here can know which org it came from.
    """
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    kind = body.kind
    files = body.files or {}
    if kind in ("apex", "trigger") and not body.xml and "body" in files:
        body.xml = files["body"]
    elif kind in ("flow", "prompt_template", "custom_object", "custom_field") and not body.xml and files:
        body.xml = _xml_from_zip_files(kind, body.api_name, files)
    if kind == "lwc":
        source: Any = _lwc_from_files(body.api_name, body.files or {}, body.api_version)
    else:
        if not (body.xml or "").strip():
            raise HTTPException(400, "Send the retrieved metadata in `xml`.")
        text = body.xml
        try:
            if kind == "prompt_template":
                source = parse_prompt_template(text, body.api_name)
            elif kind == "custom_object":
                source = parse_custom_object(text, body.api_name)
            elif kind == "custom_field":
                obj, _, fld = body.api_name.partition(".")
                if not obj or not fld:
                    raise HTTPException(400, f"{body.api_name!r} is not Object.Field__c.")
                source = parse_custom_field(text, obj, fld)
            else:
                source = text  # flow XML (parsed in _adopt_import) or Apex source
        except (UnsupportedObject, UnsupportedTemplate) as exc:
            raise HTTPException(422, str(exc)) from exc

    return _adopt_import(
        ImportRequest(
            api_name=body.api_name, kind=kind, provider=body.provider, model=body.model,
            effort=body.effort, api_version=body.api_version,
        ),
        provider, source, "the org", chosen=body.fields, stateless_pick=True,
    )


class SurveyScoreRequest(BaseModel):
    flows: Dict[str, str]


@app.post("/api/survey/score")
def survey_score(body: SurveyScoreRequest) -> Dict[str, Any]:
    """The scoring half of /api/survey/start: flows the extension retrieved -> the same report."""
    if not body.flows:
        raise HTTPException(400, "Send at least one flow in `flows` ({name: xml}).")
    survey = Survey()
    for name, xml in body.flows.items():
        survey.add(name, xml)
    return {"report": text_report(survey), "total": survey.total, "parsed": len(survey.parsed)}


@app.post("/api/explain/start")
async def explain_start(body: ExplainRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if session.pending_explain is not None and not session.pending_explain.done():
        raise HTTPException(409, "An explain is already running for this flow.")
    subject = session.artifact
    session.pending_explain = asyncio.create_task(
        asyncio.to_thread(session.generator.explain, subject, body.question)
    )
    return {"started": True}


@app.get("/api/explain/status")
async def explain_status(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    task = session.pending_explain
    if task is None:
        raise HTTPException(400, "No explain in progress - call /api/explain/start first.")
    if not task.done():
        return waiting(session.generator.provider)
    session.pending_explain = None
    explanation = llm_result(task)
    return {
        "done": True,
        "explanation": explanation,
        "usage": session.generator.provider.usage.as_dict(),
    }


def _start_llm(session: Session, func, *args, note: str) -> None:
    if session.pending_llm is not None and not session.pending_llm.task.done():
        raise HTTPException(409, "Another request is already running for this flow.")
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    session.pending_llm = PendingLLM(task=task, note=note)


def _llm_status(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    pending = session.pending_llm
    if pending is None:
        raise HTTPException(400, "Nothing in progress - call the matching /start endpoint first.")
    if not pending.task.done():
        return waiting(session.generator.provider)
    session.pending_llm = None
    result = llm_result(pending.task)
    session.record(result, pending.note)
    return {"done": True, **view(session_id, session)}


@app.post("/api/refine/start")
async def refine_start(body: RefineRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not body.instruction.strip():
        raise HTTPException(400, "Say what should change.")
    _start_llm(
        session, session.generator.refine, session.result, body.instruction,
        note=body.instruction,
    )
    return {"started": True}


@app.get("/api/refine/status")
async def refine_status(session_id: str) -> Dict[str, Any]:
    return _llm_status(session_id)


@app.post("/api/approve")
def approve(body: ApproveRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    # Approving by version means approving a stale graph is impossible: if the
    # artifact changed since the browser rendered it, the numbers no longer match.
    if body.version != session.version:
        raise HTTPException(
            409, "This changed since you looked at it. Review it again."
        )
    session.approved_version = session.version
    return view(body.session_id, session)


def _deploy_files(session: Session) -> "tuple[Dict[str, str], Dict[str, List[str]]]":
    """
    The member files and package.xml types for whatever this session holds.
    Both artifact kinds go through the same `validate_bundle` deploy call
    below - a bundle of one member is just the single-artifact case of the
    multi-type plan deploy.
    """
    if session.kind == "apex":
        apex = tag_value(session.apex, session.reference)
        body, meta = generate_apex(apex)
        return (
            {
                f"classes/{apex.api_name}.cls": body,
                f"classes/{apex.api_name}.cls-meta.xml": meta,
            },
            {"ApexClass": [apex.api_name]},
        )
    if session.kind == "trigger":
        trigger = tag_value(session.trigger, session.reference)
        body, meta = generate_apex_trigger(trigger)
        return (
            {
                f"triggers/{trigger.api_name}.trigger": body,
                f"triggers/{trigger.api_name}.trigger-meta.xml": meta,
            },
            {"ApexTrigger": [trigger.api_name]},
        )
    if session.kind == "lwc":
        component = tag_value(session.lwc, session.reference)
        return generate_lwc(component), {"LightningComponentBundle": [component.api_name]}
    flow = tag_value(session.flow, session.reference)
    return {f"flows/{flow.api_name}.flow": generate_xml(flow)}, {"Flow": [flow.api_name]}


def _start_deploy(
    session: Session,
    org: Optional[str],
    instance_url: Optional[str],
    access_token: Optional[str],
    check_only: bool,
) -> None:
    if session.pending_deploy is not None and not session.pending_deploy.task.done():
        raise HTTPException(409, "A validate/deploy is already running for this.")
    url, token = credentials(org, instance_url, access_token)
    files, types = _deploy_files(session)
    task = asyncio.create_task(
        validate_bundle(
            url, token, files, types,
            api_version=session.api_version,
            check_only=check_only,
        )
    )
    session.pending_deploy = PendingDeploy(task=task, instance_url=url, token=token)


def _failures(result) -> List[str]:
    failures = [str(failure) for failure in result.failures]
    if not failures and result.error_message:
        failures = [result.error_message]
    return failures


@app.post("/api/validate/start")
async def validate_start(body: OrgRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the flow before validating it.")
    _start_deploy(session, body.org, body.instance_url, body.access_token, check_only=True)
    return {"started": True}


@app.get("/api/validate/status")
async def validate_status(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    pending = session.pending_deploy
    if pending is None:
        raise HTTPException(400, "No validation in progress - call /api/validate/start first.")
    if not pending.task.done():
        return {"done": False}
    session.pending_deploy = None
    result = pending.task.result()
    failures = _failures(result)
    session.last_failures = failures
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "checked_version": session.version,
    }


@app.post("/api/repair/start")
async def repair_start(body: OrgRequest) -> Dict[str, Any]:
    """Feed the last validation failures back to the model."""
    session = get_session(body.session_id)
    failures = session.last_failures
    if not failures:
        raise HTTPException(400, "Nothing to repair - run a validation first.")
    _start_llm(
        session, session.generator.repair_from_salesforce, session.result, failures,
        note="Repaired from Salesforce errors",
    )
    return {"started": True}


@app.get("/api/repair/status")
async def repair_status(session_id: str) -> Dict[str, Any]:
    return _llm_status(session_id)


@app.post("/api/deploy/start")
async def deploy_start(body: DeployRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the flow before deploying it.")
    if not body.confirm:
        raise HTTPException(400, "Deploying needs an explicit confirmation.")
    _start_deploy(session, body.org, body.instance_url, body.access_token, check_only=False)
    return {"started": True}


@app.get("/api/deploy/status")
async def deploy_status(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    pending = session.pending_deploy
    if pending is None:
        raise HTTPException(400, "No deploy in progress - call /api/deploy/start first.")
    if not pending.task.done():
        return {"done": False}
    session.pending_deploy = None
    result = pending.task.result()
    failures = _failures(result)
    link = None
    if result.success:
        link = await component_setup_url(
            pending.instance_url, pending.token, session.kind, session.artifact_name,
            api_version=session.api_version,
        )
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "instance_url": pending.instance_url,
        "flow_url": link,
    }


@app.get("/api/session/{session_id}")
def session_view(session_id: str) -> Dict[str, Any]:
    """
    Re-fetch a session's current state. Exists so the browser can restore the
    flow it had on screen after something reloads the page out from under it
    - the OAuth login redirect, or a plain F5 - without that being a reason
    to lose an unsaved design. The session itself already lives here, in
    memory, independent of the browser; this just lets the browser ask for
    it again.
    """
    session = get_session(session_id)
    return view(session_id, session)


class DeployResultRequest(BaseModel):
    """What the extension saw when it validated/deployed the bundle itself."""

    success: bool
    status: Optional[str] = None
    check_only: bool = False
    failures: List[str] = []
    # Plan sessions only: each failure with the component it names, so a repair can route it.
    component_failures: List[Dict[str, str]] = []
    deployed: Optional[bool] = None  # default: a successful run that was not check-only
    version: Optional[int] = None


@app.get("/api/session/{session_id}/bundle")
def session_bundle(session_id: str) -> Dict[str, Any]:
    """
    The approved version's deploy bundle, for the extension to validate/deploy
    with the browser's own org session. Registered before the generic
    `/{artifact}` route below, which would otherwise swallow it.
    """
    session = get_session(session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the flow before fetching its bundle.")
    files, types = _deploy_files(session)
    return {
        "version": session.version, "files": files, "types": types, "api_version": session.api_version,
        "kind": session.kind, "name": session.artifact_name,
    }


@app.post("/api/session/{session_id}/deploy-result")
def session_deploy_result(session_id: str, body: DeployResultRequest) -> Dict[str, Any]:
    """Records a validation the extension ran, so the repair loop (`last_failures`) keeps working."""
    session = get_session(session_id)
    session.last_failures = list(body.failures) if not body.success else []
    return {"recorded": True, "failures": session.last_failures}


@app.get("/api/session/{session_id}/{artifact}")
def artifact(session_id: str, artifact: str) -> PlainTextResponse:
    session = get_session(session_id)

    if artifact == "package":
        files, types = _deploy_files(session)
        return _zip_response(
            build_deploy_package(files, types, session.api_version),
            f"package-{session_id}.zip",
        )

    if session.kind in ("apex", "trigger"):
        component = session.artifact

        if artifact == "report":
            status = "approved" if session.approved else "not yet approved"
            # PlanStep's artifact_type only recognizes "apex" - a trigger
            # reuses that label here since it is purely a display attribute
            # in this report, not a dispatch key (isinstance(value, ...) is
            # what picks the rendering below).
            step = StepResult(
                step=PlanStep(artifact_type="apex", name=component.api_name, brief=""),
                value=component, repairs=0, messages=[],
            )
            report = render_standalone_report(
                [step], title=f"{component.api_name} - v{session.version}",
                meta=f"{len(component.body.splitlines())} line(s) - {status}"
            + (f" - Ref: {session.reference}" if session.reference else ""),
            )
            return PlainTextResponse(
                report, media_type="text/html",
                headers={
                    "Content-Disposition": f'attachment; filename="{session.kind}-{session_id}.html"'
                },
            )

        bodies = {
            "xml": (component.body, "text/plain"),
            "ir": (component.model_dump_json(exclude_none=True, indent=2), "application/json"),
        }
        if artifact not in bodies:
            raise HTTPException(404, f"No such artifact: {artifact}")
        text, media_type = bodies[artifact]
        return PlainTextResponse(text, media_type=media_type)

    if session.kind == "lwc":
        component = session.lwc

        if artifact == "report":
            status = "approved" if session.approved else "not yet approved"
            step = StepResult(
                step=PlanStep(artifact_type="lwc", name=component.api_name, brief=""),
                value=component, repairs=0, messages=[],
            )
            report = render_standalone_report(
                [step], title=f"{component.api_name} - v{session.version}",
                meta=f"{len(component.js.splitlines())} line(s) of js - {status}"
            + (f" - Ref: {session.reference}" if session.reference else ""),
            )
            return PlainTextResponse(
                report, media_type="text/html",
                headers={
                    "Content-Disposition": f'attachment; filename="lwc-{session_id}.html"'
                },
            )

        # Per-file artifact keys, not the single "xml" key Apex/trigger use -
        # a bundle has no one body to hand back.
        bodies = {
            "js": (component.js, "text/plain"),
            "html": (component.html, "text/plain"),
            "meta": (generate_lwc(component)[f"lwc/{component.api_name}/{component.api_name}.js-meta.xml"], "application/xml"),
            "ir": (component.model_dump_json(exclude_none=True, indent=2), "application/json"),
        }
        if component.css is not None:
            bodies["css"] = (component.css, "text/plain")
        if artifact not in bodies:
            raise HTTPException(404, f"No such artifact: {artifact}")
        text, media_type = bodies[artifact]
        return PlainTextResponse(text, media_type=media_type)

    flow = session.flow

    if artifact == "report":
        # Same downloadable-standalone-document idea as /api/plan/session's
        # report - a single-Flow session wrapped as a one-step "plan" so it
        # can reuse the identical renderer (live mermaid diagram + the same
        # per-element detail table the Diagram tab's side panel shows),
        # rather than a second HTML template drifting from that one.
        status = "approved" if session.approved else "not yet approved"
        step = StepResult(
            step=PlanStep(artifact_type="flow", name=flow.label, brief=""),
            value=flow, repairs=0, messages=[],
        )
        report = render_standalone_report(
            [step], title=f"{flow.label} - v{session.version}",
            meta=f"{len(flow.elements)} element(s) - {status}"
            + (f" - Ref: {session.reference}" if session.reference else ""),
        )
        return PlainTextResponse(
            report, media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="flow-{session_id}.html"'},
        )

    bodies = {
        "xml": (generate_xml(flow), "application/xml"),
        "markdown": (to_markdown(flow), "text/markdown"),
        "test": (to_test_guide(flow), "text/markdown"),
        "ir": (flow.model_dump_json(exclude_none=True, indent=2), "application/json"),
    }
    if artifact not in bodies:
        raise HTTPException(404, f"No such artifact: {artifact}")
    text, media_type = bodies[artifact]
    return PlainTextResponse(text, media_type=media_type)



# --------------------------------------------------------------------------
# Multi-artifact plans (Object / Field / Apex / Flow, one request)
# --------------------------------------------------------------------------
#
# Additive on purpose: nothing above this point changes. A single-Flow
# request still goes through /api/design/* exactly as before; this is the
# separate path for "an object with a field and a flow that uses it" -
# planner.py decides how many steps that needs and of what type, and each
# step runs through the same generator /api/design/* already uses under the
# hood (FlowGenerator, CustomObjectGenerator, ...). See the multi-artifact
# plan doc for why this stays a second path rather than replacing Session:
# splitting the risk of this rework from the tool's existing, working one.


class PlanRequest(BaseModel):
    request: str
    provider: Optional[str] = None
    model: Optional[str] = None
    # `model` plans; each step is generated by a cheaper model picked for the
    # provider (see pick_worker_model). Off keeps one model for everything.
    auto_workers: bool = False
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "64.0"
    api_key: Optional[str] = None
    # Opt-in Permission Set grant for whatever object/field steps this plan
    # produces - "new" creates one, "existing" merges into
    # permission_set_api_name. None (the default) means no grant at all.
    grant_permission_set: Optional[Literal["new", "existing"]] = None
    permission_set_label: Optional[str] = None
    permission_set_api_name: Optional[str] = None
    # Username of the user an Agentforce agent runs as ("Einstein Agent User"
    # profile). Only needed when the plan builds an agent.
    agent_user: Optional[str] = None
    # The person's standing conventions (naming, tests, style), applied to every step.
    house_rules: Optional[str] = None
    # What the extension read from the org, since this server cannot: {"objects": {"Account":
    # {"fields": ["Region__c", ...]}}}. Handed to the planner as facts about the org.
    org_context: Optional[Dict[str, Any]] = None


class PlanExecuteRequest(BaseModel):
    plan_id: str
    # Off by default - see execute_plan's docstring for why. Exposed as a UI
    # toggle so it can still be compared against, not removed outright.
    parallel: bool = False


class PlanStepReviseRequest(BaseModel):
    session_id: str
    step_name: str
    instruction: str


class PlanReviseRequest(BaseModel):
    session_id: str
    instruction: str


@dataclass
class PermissionSetConfig:
    """The opt-in Permission Set grant a plan can carry, threaded through
    PendingPlan -> StoredPlan -> PendingPlanExecution -> PlanSession the same
    way api_version already is end to end. Not a PlanStep - see
    ir_permset.py's module docstring for why."""

    mode: Literal["new", "existing"]
    label: Optional[str] = None
    api_name: Optional[str] = None


@dataclass
class PendingPlan:
    """A planning call running in the background - same shape as PendingDesign,
    one level up: this produces a Plan, not yet any generated metadata."""

    task: "asyncio.Task"
    provider: Provider
    api_version: str
    permission_set_config: Optional[PermissionSetConfig] = None
    agent_user: Optional[str] = None
    house_rules: Optional[str] = None


PLAN_JOBS: Dict[str, PendingPlan] = {}


@dataclass
class StoredPlan:
    """
    A validated Plan awaiting execution. Kept separate from PlanSession
    because a plan is cheap to produce and worth showing the user before
    spending a generation call per step - the browser can look at the
    decomposition and back out before anything downstream runs.
    """

    provider: Provider
    plan: Plan
    # The planning conversation that produced `plan` - kept so a later
    # /api/plan/revise/start can continue it (see revise_plan in planner.py)
    # instead of re-planning from the original request with no memory of
    # what it already decided.
    messages: List[Message]
    api_version: str
    permission_set_config: Optional[PermissionSetConfig] = None


PLANS: Dict[str, StoredPlan] = {}


@dataclass
class PendingPlanExecution:
    task: "asyncio.Task"
    provider: Provider
    plan: Plan
    messages: List[Message]
    api_version: str
    permission_set_config: Optional[PermissionSetConfig] = None


PLAN_EXECUTIONS: Dict[str, PendingPlanExecution] = {}


@dataclass
class PlanSession:
    provider: Provider
    plan: Plan
    steps: List[StepResult]
    activate: bool = False
    api_version: str = "64.0"
    version: int = 1
    approved_version: int = 0
    permission_set_config: Optional[PermissionSetConfig] = None
    reference: Optional[str] = None
    # See StoredPlan.messages - carried through so a plan-level revise can
    # still continue the original planning conversation after execute.
    planner_messages: List[Message] = field(default_factory=list)
    pending_deploy: Optional[PendingDeploy] = None
    pending_llm: Optional[PendingLLM] = None
    # What the org said last time validate failed. `last_failures` is the
    # human-readable form the UI shows; `last_component_failures` keeps each
    # failure's own component name (ComponentProblem.full_name) alongside it,
    # so a repair can route each failure to the step it actually names
    # instead of re-generating the whole plan - see _repair_plan.
    last_failures: List[str] = field(default_factory=list)
    last_component_failures: List[Any] = field(default_factory=list)
    # The plan version a real deploy last succeeded for: an employee agent can be
    # published from its step only while the deployed bundle is the one on screen.
    deployed_version: int = 0
    pending_publish: Optional["asyncio.Task"] = None
    publishing_agent: Optional[str] = None
    # Agent api name -> PublishResult.as_dict() of its last publish from here.
    agent_publish: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Generated test scenarios for the plan version in `scenarios_version`;
    # stale (and hidden) once the plan is revised.
    scenarios: Optional[TestScenarios] = None
    scenarios_version: int = 0
    pending_scenarios: Optional["asyncio.Task"] = None

    @property
    def approved(self) -> bool:
        return self.approved_version == self.version

    @property
    def deployed(self) -> bool:
        return self.deployed_version == self.version

    def apply_policy(self) -> None:
        """Same call as Session.apply_policy: status/API version are the
        tool's, never the model's - applied to every Flow step in the plan."""
        for result in self.steps:
            if isinstance(result.value, Flow):
                result.value.status = "Active" if self.activate else "Draft"
                result.value.api_version = self.api_version


PLAN_SESSIONS: Dict[str, PlanSession] = {}


def get_plan_session(session_id: str) -> PlanSession:
    session = PLAN_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown plan session. Start a new plan.")
    return session


def _step_view(result: StepResult) -> Dict[str, Any]:
    value = result.value
    entry: Dict[str, Any] = {
        "name": result.step.name,
        "artifact_type": result.step.artifact_type,
        "flow_type": result.step.flow_type,
        "depends_on": result.step.depends_on,
        "repairs": result.repairs,
        "description": getattr(value, "description", None),
        "reasoning": getattr(value, "reasoning", None),
    }
    if isinstance(value, Flow):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "element_count": len(value.elements),
            "mermaid": to_mermaid(value),
            "element_index": element_index(value),
        })
    elif isinstance(value, CustomObject):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "plural_label": value.plural_label,
        })
    elif isinstance(value, CustomField):
        entry.update({
            "api_name": value.api_name,
            "object_api_name": value.object_api_name,
            "field_type": value.type,
        })
    elif isinstance(value, ApexClass):
        entry.update({
            "api_name": value.api_name,
            "lines": len(value.body.splitlines()),
            "body": value.body,
        })
    elif isinstance(value, LightningComponent):
        entry.update({
            "api_name": value.api_name,
            "lines": len(value.js.splitlines()),
            "js": value.js,
            "html": value.html,
            "css": value.css,
            "is_exposed": value.is_exposed,
            "targets": value.targets,
            "test_guide": to_lwc_test_guide(value),
        })
    elif isinstance(value, MetadataType):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "visibility": value.visibility,
            "field_count": len(value.fields),
            "fields": [{"api_name": f.api_name, "type": f.type} for f in value.fields],
        })
    elif isinstance(value, CustomMetadataRecord):
        entry.update({
            "type_api_name": value.type_api_name,
            "developer_name": value.developer_name,
            "values": value.values,
        })
    elif isinstance(value, PlatformEvent):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "publish_behavior": value.publish_behavior,
            "field_count": len(value.fields),
            "fields": [{"api_name": f.api_name, "type": f.type} for f in value.fields],
        })
    elif isinstance(value, Agent):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "agent_type": value.agent_type,
            "instructions": value.instructions,
            "topics": [
                {
                    "api_name": t.api_name,
                    "label": t.label,
                    "description": t.description,
                    "actions": [
                        {"api_name": a.api_name, "target": f"{a.target_type}://{a.target}"}
                        for a in t.actions
                    ],
                }
                for t in value.topics
            ],
        })
    elif isinstance(value, PromptTemplate):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "response_format": value.response_format,
            "inputs": [{"name": i.name, "type": i.type} for i in value.inputs],
            "output_keys": value.output_keys(),
            "content": value.content,
        })
    return entry


def _permission_set_view(session: PlanSession) -> Optional[Dict[str, Any]]:
    """The Access Grant preview - pure data already in hand (no org contact),
    so it's available the instant execute finishes, well before deploy is
    the one place that needs org creds (to retrieve an existing Permission
    Set for merging - see _add_permission_set_files)."""
    config = session.permission_set_config
    if config is None:
        return None
    grant = build_grant_from_steps(session.steps, config.label or config.api_name or "Generated Access")
    if not grant.field_grants and not grant.object_grants:
        return None  # nothing to grant (no new object or field in this plan)
    return {
        "mode": config.mode,
        "target": config.api_name,
        "field_grants": [
            {"field_api_name": fg.field_api_name, "object_api_name": fg.object_api_name,
             "editable": fg.editable}
            for fg in grant.field_grants
        ],
        "object_grants": [
            {"object_api_name": og.object_api_name} for og in grant.object_grants
        ],
    }


def plan_view(session_id: str, session: PlanSession) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "version": session.version,
        "approved": session.approved,
        "reasoning": session.plan.reasoning,
        "how_to_test": session.plan.how_to_test,
        "reference": session.reference,
        "agent_user": session.plan.agent_user,
        "house_rules": session.plan.house_rules,
        "steps": [_step_view(r) for r in session.steps],
        "deployed": session.deployed,
        "agent_publish": session.agent_publish,
        "permission_set": _permission_set_view(session),
        "scenarios": _scenarios_view(session),
        "usage": session.provider.usage.as_dict(),
    }


def _bundle_files_and_types(
    steps: List[StepResult], reference: Optional[str] = None, agent_user: Optional[str] = None,
) -> tuple[Dict[str, str], Dict[str, List[str]]]:
    """
    Every step's generated IR, compiled to the metadata files and package.xml
    member names a single deploy needs - the same mapping verify_object_apex.py
    (a dev-QA script) builds by hand per shape, generalized here for a real
    plan's steps of mixed artifact types.

    A field whose object_api_name matches a CustomObject step in this same
    plan is embedded directly inside that object's .object file (a complete
    document); a field targeting an object this plan does not also create
    goes into a *delta* .object file holding only that object's new fields
    (generate_field_delta) - there is no separate per-field file format.
    Both were confirmed live and via Salesforce's own tooling
    (`sf project convert source`) - see xmlgen_object.py's module docstring
    for the full story, including the standalone-CustomField-file convention
    that turned out not to exist despite being a widely-repeated one.
    """
    objects: Dict[str, CustomObject] = {}
    fields_by_object: Dict[str, List[CustomField]] = {}
    mdt_types: Dict[str, MetadataType] = {}
    platform_events: Dict[str, PlatformEvent] = {}
    other_steps: List[Any] = []

    for result in steps:
        value = tag_value(result.value, reference)
        if isinstance(value, CustomObject):
            objects[value.api_name] = value
        elif isinstance(value, CustomField):
            fields_by_object.setdefault(value.object_api_name, []).append(value)
        elif isinstance(value, MetadataType):
            mdt_types[value.api_name] = value
        elif isinstance(value, PlatformEvent):
            platform_events[value.api_name] = value
        else:
            other_steps.append(value)

    files: Dict[str, str] = {}
    types: Dict[str, List[str]] = {}

    for api_name, obj in objects.items():
        files[f"objects/{api_name}.object"] = generate_object(
            obj, fields_by_object.get(api_name, [])
        )
        types.setdefault("CustomObject", []).append(api_name)

    for object_api_name, fields in fields_by_object.items():
        if object_api_name in objects:
            continue  # already embedded above
        files[f"objects/{object_api_name}.object"] = generate_field_delta(fields)
        for field in fields:
            types.setdefault("CustomField", []).append(
                f"{object_api_name}.{field.api_name}"
            )

    for api_name, mdt in mdt_types.items():
        # A __mdt type deploys under the same package.xml member type as a
        # regular Custom Object - confirmed against Salesforce's own
        # Metadata API reference, not assumed. Its fields are always embedded
        # (generate_mdt_type), never split into a delta the way CustomField
        # can be - a __mdt has no "add a field later" step in this tool.
        files[f"objects/{api_name}.object"] = generate_mdt_type(mdt)
        types.setdefault("CustomObject", []).append(api_name)

    for api_name, event in platform_events.items():
        # A platform event deploys under the same package.xml member type as
        # a regular Custom Object too - confirmed against Salesforce's own
        # Metadata API and Platform Events documentation, not assumed. Its
        # fields are always embedded (generate_platform_event), same reason
        # a __mdt type's are: no "add a field later" step exists for either.
        files[f"objects/{api_name}.object"] = generate_platform_event(event)
        types.setdefault("CustomObject", []).append(api_name)

    for value in other_steps:
        if isinstance(value, Flow):
            files[f"flows/{value.api_name}.flow"] = generate_xml(value)
            types.setdefault("Flow", []).append(value.api_name)
        elif isinstance(value, ApexClass):
            body, meta = generate_apex(value)
            files[f"classes/{value.api_name}.cls"] = body
            files[f"classes/{value.api_name}.cls-meta.xml"] = meta
            types.setdefault("ApexClass", []).append(value.api_name)
        elif isinstance(value, PromptTemplate):
            files[f"genAiPromptTemplates/{value.api_name}.genAiPromptTemplate"] = (
                generate_prompt_template(value)
            )
            types.setdefault("GenAiPromptTemplate", []).append(value.api_name)
        elif isinstance(value, Agent):
            files.update(generate_agent_files(value, agent_user))
            types.setdefault("AiAuthoringBundle", []).append(value.api_name)
        elif isinstance(value, LightningComponent):
            files.update(generate_lwc(value))
            types.setdefault("LightningComponentBundle", []).append(value.api_name)
        elif isinstance(value, CustomMetadataRecord):
            mdt_type = mdt_types.get(value.type_api_name)
            files[f"customMetadata/{value.type_api_name}.{value.developer_name}.md"] = (
                generate_mdt_record(value, mdt_type)
            )
            types.setdefault("CustomMetadata", []).append(
                f"{value.type_api_name}.{value.developer_name}"
            )

    return files, types


def _org_context_text(context: Optional[Dict[str, Any]]) -> str:
    """Existing objects/fields the caller read from the org, as text for the planner's request."""
    objects = (context or {}).get("objects")
    if not isinstance(objects, dict) or not objects:
        return ""
    lines = ["", "", "Already in the org (do not recreate these; build on them):"]
    for name, info in list(objects.items())[:50]:
        fields = info.get("fields", []) if isinstance(info, dict) else []
        names = ", ".join(str(f.get("api_name", f)) if isinstance(f, dict) else str(f) for f in fields[:200])
        lines.append(f"- {name}" + (f": fields {names}" if names else ""))
    return "\n".join(lines)


@app.post("/api/plan/start")
async def plan_start(body: PlanRequest) -> Dict[str, Any]:
    """Kick off planning in the background - same reasoning as design_start:
    the provider call is synchronous, so it runs in a thread."""
    if not body.request.strip():
        raise HTTPException(400, "Describe what should be built.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
        if body.auto_workers:
            provider = await asyncio.to_thread(
                with_auto_workers, provider, body.effort, body.api_key,
            )
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    permission_set_config = None
    if body.grant_permission_set == "new":
        label = body.permission_set_label or "Generated Access"
        permission_set_config = PermissionSetConfig(
            mode="new", label=label, api_name=sanitize_api_name(label),
        )
    elif body.grant_permission_set == "existing":
        permission_set_config = PermissionSetConfig(
            mode="existing", label=body.permission_set_label,
            api_name=body.permission_set_api_name,
        )

    generator = PlannerGenerator(planner_of(provider))
    house_rules = clean_house_rules(body.house_rules)
    request_text = body.request + _org_context_text(body.org_context) + house_rules_text(house_rules)
    task = asyncio.create_task(asyncio.to_thread(generator.generate, request_text))
    job_id = uuid.uuid4().hex
    PLAN_JOBS[job_id] = PendingPlan(
        task=task, provider=provider, api_version=body.api_version,
        permission_set_config=permission_set_config,
        agent_user=(body.agent_user or "").strip() or None,
        house_rules=house_rules,
    )
    return {"job_id": job_id}


@app.get("/api/plan/status")
async def plan_status(job_id: str) -> Dict[str, Any]:
    pending = PLAN_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown plan job.")
    if not pending.task.done():
        return waiting(planner_of(pending.provider))
    del PLAN_JOBS[job_id]

    result = llm_result(pending.task)
    result.value.agent_user = pending.agent_user  # a build option, never the model's to choose
    result.value.house_rules = pending.house_rules
    plan_id = uuid.uuid4().hex
    PLANS[plan_id] = StoredPlan(
        provider=pending.provider, plan=result.value, messages=result.messages,
        api_version=pending.api_version, permission_set_config=pending.permission_set_config,
    )
    steps = [
        {"name": s.name, "artifact_type": s.artifact_type, "brief": s.brief,
         "depends_on": s.depends_on, "flow_type": s.flow_type}
        for s in result.value.steps
    ]
    return {
        "done": True, "plan_id": plan_id, "steps": steps,
        "reasoning": result.value.reasoning, "how_to_test": result.value.how_to_test,
    }


@app.post("/api/plan/execute/start")
async def plan_execute_start(body: PlanExecuteRequest) -> Dict[str, Any]:
    """
    Run every step of a planned request through its generator. One request
    can take as long as N generations - a bigger plan is a bigger wait, the
    same "runs in the background, browser polls" shape as everything else
    here handles, just with more work behind one job id.
    """
    stored = PLANS.get(body.plan_id)
    if stored is None:
        raise HTTPException(404, "Unknown plan. Call /api/plan/start first.")
    del PLANS[body.plan_id]

    task = asyncio.create_task(
        asyncio.to_thread(execute_plan, stored.provider, stored.plan, parallel=body.parallel)
    )
    job_id = uuid.uuid4().hex
    PLAN_EXECUTIONS[job_id] = PendingPlanExecution(
        task=task, provider=stored.provider, plan=stored.plan, messages=stored.messages,
        api_version=stored.api_version, permission_set_config=stored.permission_set_config,
    )
    return {"job_id": job_id}


@app.get("/api/plan/execute/status")
async def plan_execute_status(job_id: str) -> Dict[str, Any]:
    pending = PLAN_EXECUTIONS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown plan execution job.")
    if not pending.task.done():
        return waiting(pending.provider)
    del PLAN_EXECUTIONS[job_id]

    steps = llm_result(pending.task)
    session_id = uuid.uuid4().hex
    session = PlanSession(
        provider=pending.provider, plan=pending.plan, steps=steps,
        api_version=pending.api_version,
        permission_set_config=pending.permission_set_config,
        planner_messages=pending.messages,
    )
    session.apply_policy()
    PLAN_SESSIONS[session_id] = session
    return {"done": True, **plan_view(session_id, session)}


class AgentUserRequest(BaseModel):
    session_id: str
    agent_user: str = ""


@app.post("/api/plan/agent-user")
def plan_set_agent_user(body: AgentUserRequest) -> Dict[str, Any]:
    """The username a service agent runs as, entered on the agent's own step once
    the plan exists. It is part of the generated script, so changing it means the
    plan has to be approved and validated again."""
    session = get_plan_session(body.session_id)
    value = body.agent_user.strip() or None
    if value != session.plan.agent_user:
        session.plan.agent_user = value
        session.version += 1
    return plan_view(body.session_id, session)


@app.post("/api/plan/approve")
def plan_approve(body: ApproveRequest) -> Dict[str, Any]:
    session = get_plan_session(body.session_id)
    if body.version != session.version:
        raise HTTPException(
            409, "The plan changed since you looked at it. Review it again."
        )
    session.approved_version = session.version
    return plan_view(body.session_id, session)


async def _add_permission_set_files(
    config: PermissionSetConfig,
    steps: List[StepResult],
    url: str,
    token: str,
    api_version: str,
    files: Dict[str, str],
    types: Dict[str, List[str]],
    reference: Optional[str] = None,
) -> None:
    """
    Renders the opt-in Permission Set grant and folds it into `files`/`types`
    in place - "new" needs no org contact (a fresh document); "existing"
    retrieves the current .permissionset XML first and merges into it. Kept
    separate from _bundle_files_and_types (which stays sync/org-free) since
    this is the one part of a plan's deploy that can need an org round trip
    before the bundle is complete.
    """
    existing_xml = None
    if config.mode == "existing" and _permission_set_grant(config, steps, reference) is not None:
        existing_xml = await retrieve_permission_set(url, token, config.api_name, api_version=api_version)
    _add_permission_set_xml(config, steps, existing_xml, files, types, reference)


def _permission_set_grant(
    config: PermissionSetConfig, steps: List[StepResult], reference: Optional[str],
) -> Optional[PermissionSetGrant]:
    grant = build_grant_from_steps(steps, config.label or config.api_name or "Generated Access")
    if not grant.field_grants and not grant.object_grants:
        return None  # a plan with no new object or field has nothing to grant - no empty Permission Set
    if reference:
        grant.description = tag_description(grant.description, reference)
    return grant


def _add_permission_set_xml(
    config: PermissionSetConfig,
    steps: List[StepResult],
    existing_xml: Optional[str],
    files: Dict[str, str],
    types: Dict[str, List[str]],
    reference: Optional[str] = None,
) -> None:
    """The org-free part: render (or merge into `existing_xml`, which the caller fetched)."""
    grant = _permission_set_grant(config, steps, reference)
    if grant is None:
        return
    api_name = config.api_name
    if config.mode == "new":
        xml = generate_permission_set(grant, api_name=api_name)
    else:
        if existing_xml is None:
            raise HTTPException(400, f"Merging into {api_name} needs its current XML (existing_permission_set_xml).")
        xml = merge_permission_set_xml(existing_xml, grant)

    files[f"permissionsets/{api_name}.permissionset"] = xml
    types.setdefault("PermissionSet", []).append(api_name)


# An Agentforce agent is an AiAuthoringBundle, which the Metadata API only accepts
# from API 65.0 ("Not available for deploy for this API version" at 62 and 64).
_AGENT_MIN_API_VERSION = "65.0"


# A prompt template's response format (JSON) only exists in the metadata from API 64.0:
# retrieved or deployed at 62.0 it silently comes back as plain Text (checked on a scratch org).
_PROMPT_TEMPLATE_MIN_API_VERSION = "64.0"


def _deploy_api_version(session: "PlanSession") -> str:
    """The API version a plan's bundle deploys with: the session's, raised to the
    minimum its newest component type needs."""
    version = session.api_version
    if any(isinstance(r.value, Agent) for r in session.steps):
        version = max(version, _AGENT_MIN_API_VERSION, key=float)
    if any(isinstance(r.value, PromptTemplate) for r in session.steps):
        version = max(version, _PROMPT_TEMPLATE_MIN_API_VERSION, key=float)
    return version


async def _deploy_plan_bundle(
    session: PlanSession, url: str, token: str, check_only: bool,
) -> Any:
    files, types = _bundle_files_and_types(session.steps, session.reference, session.plan.agent_user)
    api_version = _deploy_api_version(session)
    if session.permission_set_config is not None:
        await _add_permission_set_files(
            session.permission_set_config, session.steps, url, token,
            api_version, files, types, session.reference,
        )
    result = await validate_bundle(
        url, token, files, types, api_version=api_version, check_only=check_only,
    )
    # A real deploy leaves each Prompt Template Published but not callable: it needs
    # its active version named, which only exists once the org has created it.
    templates = [r.value.api_name for r in session.steps if isinstance(r.value, PromptTemplate)]
    if result.success and not check_only and templates:
        try:
            async with MetadataClient(url, token, api_version) as client:
                await activate_templates(client, templates, api_version)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden: the plan is half done
            result.success = False
            result.failures.append(ComponentProblem(
                full_name=", ".join(templates), problem_type="Error",
                problem=f"Deployed, but the prompt template could not be activated ({exc}). "
                        "Activate its version in Prompt Builder before a Flow calls it.",
            ))
    return result


def _require_agent_user(session: "PlanSession") -> None:
    needs_user = any(isinstance(r.value, Agent) and r.value.agent_type == "service" for r in session.steps)
    if needs_user and not (session.plan.agent_user or "").strip():
        raise HTTPException(
            400,
            "This plan builds a service agent, which needs an agent user: the username of a user with the "
            "'Einstein Agent User' profile. Enter it in the Agent user box on the agent step, then approve and validate again.",
        )


def _start_plan_deploy(
    session: PlanSession,
    org: Optional[str],
    instance_url: Optional[str],
    access_token: Optional[str],
    check_only: bool,
) -> None:
    if session.pending_deploy is not None and not session.pending_deploy.task.done():
        raise HTTPException(409, "A validate/deploy is already running for this plan.")
    _require_agent_user(session)
    url, token = credentials(org, instance_url, access_token)
    task = asyncio.create_task(_deploy_plan_bundle(session, url, token, check_only))
    session.pending_deploy = PendingDeploy(task=task, instance_url=url, token=token)


# --- Compare with the org ----------------------------------------------------
# Read-only: for each step, is it new in the org or does it exist, and what would
# deploying change. Runs on its own job so it never delays or blocks a validate.

COMPARES: Dict[str, "asyncio.Task"] = {}


async def _compare_plan(session: PlanSession, url: str, token: str) -> List[Dict[str, Any]]:
    api_version = _deploy_api_version(session)
    async with OrgReader(url, token, api_version) as reader:
        async with MetadataClient(url, token, api_version) as meta:
            return await compare_steps(reader, session.steps, meta)


@app.post("/api/plan/compare/start")
async def plan_compare_start(body: OrgRequest) -> Dict[str, Any]:
    session = get_plan_session(body.session_id)
    url, token = credentials(body.org, body.instance_url, body.access_token)
    job_id = uuid.uuid4().hex
    COMPARES[job_id] = asyncio.create_task(_compare_plan(session, url, token))
    return {"job_id": job_id}


@app.get("/api/plan/compare/status")
async def plan_compare_status(job_id: str) -> Dict[str, Any]:
    task = COMPARES.get(job_id)
    if task is None:
        raise HTTPException(404, "Unknown compare job.")
    if not task.done():
        return {"done": False}
    del COMPARES[job_id]
    try:
        return {"done": True, "items": task.result()}
    except Exception as exc:  # noqa: BLE001 - the whole comparison could not run (connection, token)
        raise HTTPException(400, f"Could not compare with the org: {exc}") from exc


@app.post("/api/plan/validate/start")
async def plan_validate_start(body: OrgRequest) -> Dict[str, Any]:
    session = get_plan_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the plan before validating it.")
    _start_plan_deploy(session, body.org, body.instance_url, body.access_token, check_only=True)
    return {"started": True}


@app.get("/api/plan/validate/status")
async def plan_validate_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    pending = session.pending_deploy
    if pending is None:
        raise HTTPException(400, "No validation in progress - call /api/plan/validate/start first.")
    if not pending.task.done():
        return {"done": False}
    session.pending_deploy = None
    result = pending.task.result()
    failures = _failures(result)
    session.last_failures = failures
    session.last_component_failures = list(result.failures)
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "checked_version": session.version,
    }


@app.post("/api/plan/deploy/start")
async def plan_deploy_start(body: DeployRequest) -> Dict[str, Any]:
    session = get_plan_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the plan before deploying it.")
    if not body.confirm:
        raise HTTPException(400, "Deploying needs an explicit confirmation.")
    _start_plan_deploy(session, body.org, body.instance_url, body.access_token, check_only=False)
    return {"started": True}


@app.get("/api/plan/deploy/status")
async def plan_deploy_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    pending = session.pending_deploy
    if pending is None:
        raise HTTPException(400, "No deploy in progress - call /api/plan/deploy/start first.")
    if not pending.task.done():
        return {"done": False}
    session.pending_deploy = None
    result = pending.task.result()
    urls: Dict[str, str] = {}
    if result.success:
        session.deployed_version = session.version
        urls = await _plan_component_urls(session, pending.instance_url, pending.token)
        if session.permission_set_config is not None:
            urls["Permission Set"] = await component_setup_url(
                pending.instance_url, pending.token, "permission_set",
                session.permission_set_config.api_name,
                api_version=session.api_version,
            )
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": _failures(result),
        "instance_url": pending.instance_url,
        "setup_urls": urls,
        "plan": plan_view(session_id, session),
    }


def _scenarios_view(session: PlanSession) -> Optional[Dict[str, Any]]:
    if session.scenarios is None or session.scenarios_version != session.version:
        return None
    return session.scenarios.model_dump(exclude_none=True)


class SessionRequest(BaseModel):
    session_id: str


@app.post("/api/plan/scenarios/start")
async def plan_scenarios_start(body: SessionRequest) -> Dict[str, Any]:
    """One extra model call over the built components; no org needed."""
    session = get_plan_session(body.session_id)
    if session.pending_scenarios is not None:
        raise HTTPException(409, "Test scenarios are already being generated for this plan.")
    generator = ScenarioGenerator(session.provider)
    request = session.reference or (session.planner_messages[0].content if session.planner_messages else None)
    session.pending_scenarios = asyncio.create_task(
        asyncio.to_thread(generator.generate, session.plan, session.steps, request)
    )
    return {"started": True}


@app.get("/api/plan/scenarios/status")
async def plan_scenarios_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    task = session.pending_scenarios
    if task is None:
        raise HTTPException(400, "No scenario generation in progress - call /api/plan/scenarios/start first.")
    if not task.done():
        return waiting(session.provider)
    session.pending_scenarios = None
    result = llm_result(task)
    session.scenarios = result.value
    session.scenarios_version = session.version
    return {"done": True, "scenarios": _scenarios_view(session), "usage": session.provider.usage.as_dict()}


@app.get("/api/plan/session/{session_id}/scenarios/{index}")
async def plan_scenario_script(session_id: str, index: int) -> Dict[str, Any]:
    """
    One scenario's script, for a client that runs it itself (the browser
    extension talks to the org directly, so the hosted server never does).
    No org contact here - the same refusals as /run apply on the client side.
    """
    session = get_plan_session(session_id)
    if session.scenarios is None or session.scenarios_version != session.version:
        raise HTTPException(400, "Generate test scenarios for this plan version first.")
    if not 0 <= index < len(session.scenarios.scenarios):
        raise HTTPException(404, "No such scenario.")
    return {
        "apex": session.scenarios.scenarios[index].apex,
        "deployed": session.deployed,
        "api_version": session.api_version,
    }


class RunScenarioRequest(OrgRequest):
    index: int


@app.post("/api/plan/scenarios/run")
async def plan_scenarios_run(body: RunScenarioRequest) -> Dict[str, Any]:
    """
    Run one scenario as Anonymous Apex, rolled back. Sandbox and Developer/scratch
    orgs only, and only once this plan version is deployed there - the scenario
    exercises the real components.
    """
    session = get_plan_session(body.session_id)
    if session.scenarios is None or session.scenarios_version != session.version:
        raise HTTPException(400, "Generate test scenarios for this plan version first.")
    if not session.deployed:
        raise HTTPException(409, "Deploy this plan version first - scenarios run against the real components.")
    if not 0 <= body.index < len(session.scenarios.scenarios):
        raise HTTPException(404, "No such scenario.")
    url, token = credentials(body.org, body.instance_url, body.access_token)
    try:
        if await anon_apex.is_production(url, token, session.api_version):
            raise HTTPException(403, "Scenarios only run in a sandbox or scratch org, never production.")
        run = await anon_apex.run_anonymous(
            url, token, session.scenarios.scenarios[body.index].apex, session.api_version,
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return run.as_dict()


class AgentPublishRequest(OrgRequest):
    agent: str


@app.post("/api/plan/agent/publish/start")
async def plan_agent_publish_start(body: AgentPublishRequest) -> Dict[str, Any]:
    """Publish and activate an employee agent this plan just deployed - what
    `sf agent publish authoring-bundle` + `sf agent activate` do, over HTTP (see
    flowtool/agent_publish.py). Service agents stay manual in Agentforce Builder."""
    session = get_plan_session(body.session_id)
    agent = next(
        (r.value for r in session.steps if isinstance(r.value, Agent) and r.value.api_name == body.agent), None,
    )
    if agent is None:
        raise HTTPException(404, f"This plan has no agent named {body.agent}.")
    if agent.agent_type != "employee":
        raise HTTPException(
            400, "Only employee agents can be published from here. Publish a service agent in Agentforce Builder.",
        )
    if not session.deployed:
        raise HTTPException(403, "Deploy the plan before publishing its agent.")
    if session.pending_publish is not None and not session.pending_publish.done():
        raise HTTPException(409, "A publish is already running for this plan.")
    url, token = credentials(body.org, body.instance_url, body.access_token)
    session.pending_publish = asyncio.create_task(
        publish_employee_agent(url, token, agent.api_name, api_version=_deploy_api_version(session)),
    )
    session.publishing_agent = agent.api_name
    return {"started": True}


@app.get("/api/plan/agent/publish/status")
async def plan_agent_publish_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    task = session.pending_publish
    if task is None:
        raise HTTPException(400, "No publish in progress - call /api/plan/agent/publish/start first.")
    if not task.done():
        return {"done": False}
    session.pending_publish = None
    try:
        result = task.result()
    except AgentPublishError as exc:
        return {"done": True, "success": False, "agent": session.publishing_agent, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a network failure is shown, not a 500
        return {"done": True, "success": False, "agent": session.publishing_agent,
                "error": f"{type(exc).__name__}: {exc}"}
    session.agent_publish[result.agent] = result.as_dict()
    return {"done": True, "success": True, **result.as_dict(), "plan": plan_view(session_id, session)}


async def _plan_component_urls(
    session: "PlanSession", instance_url: str, token: str,
) -> Dict[str, str]:
    """
    A direct Setup link per step, keyed by step name - so a deploy that just
    created several components doesn't leave a person hunting through Setup
    to find what actually appeared. Resolved after a successful deploy, same
    as the single-Flow path's flow_url.
    """
    async def one(result: StepResult) -> tuple:
        value = result.value
        if isinstance(value, CustomObject):
            url = await component_setup_url(
                instance_url, token, "object", value.api_name,
                api_version=session.api_version,
            )
        elif isinstance(value, CustomField):
            url = await component_setup_url(
                instance_url, token, "field", value.api_name,
                api_version=session.api_version, object_api_name=value.object_api_name,
            )
        elif isinstance(value, ApexClass):
            url = await component_setup_url(
                instance_url, token, "apex", value.api_name,
                api_version=session.api_version,
            )
        elif isinstance(value, Flow):
            url = await component_setup_url(
                instance_url, token, "flow", value.api_name,
                api_version=session.api_version,
            )
        elif isinstance(value, LightningComponent):
            url = await component_setup_url(
                instance_url, token, "lwc", value.api_name,
                api_version=session.api_version,
            )
        else:
            return result.step.name, None
        return result.step.name, url

    pairs = await asyncio.gather(*(one(r) for r in session.steps))
    return {name: url for name, url in pairs if url}


def _repair_plan(
    provider: Provider, steps: List[StepResult], failures: List[str],
    component_failures: List[Any],
) -> List[StepResult]:
    """
    Route each failure to the step it actually names, and re-run only that
    step through repair_step - a step no failure names comes back unchanged,
    so a plan with one bad Apex class doesn't also spend a generation call
    re-rolling the Object and Field steps that already deployed cleanly.

    Routing is by ComponentProblem.full_name, not a text search over the
    rendered failure string: an object's api_name can appear as a plain
    substring of an unrelated field's own failure text (e.g. "Invoice__c" is
    a substring of "Field does not exist: Amount__c on Invoice__c"), which a
    naive `api_name in failure_text` match would wrongly also route to the
    Object step. full_name is Salesforce's own answer to "which component is
    this error about" - an Apex compile error's full_name is the class, even
    when the underlying cause is a field another step creates; a
    CustomField-specific failure's full_name may be dotted
    ("Object__c.Field__c"), so a step's api_name is matched as either the
    whole full_name or the part after the last dot.

    When there is no structured full_name to go on (an org-level
    error_message with no per-component detail), every step is retried with
    the same undifferentiated failure text - there is nothing to route by.
    """
    updated: List[StepResult] = []
    for step in steps:
        if component_failures:
            matched = [
                str(problem) for problem in component_failures
                if problem.full_name == step.value.api_name
                or problem.full_name.endswith(f".{step.value.api_name}")
            ]
        else:
            matched = failures
        updated.append(repair_step(provider, step, matched) if matched else step)
    return updated


@app.post("/api/plan/repair/start")
async def plan_repair_start(body: OrgRequest) -> Dict[str, Any]:
    """Feed the last validation/deploy failures back to whichever step(s) they name."""
    session = get_plan_session(body.session_id)
    failures = session.last_failures
    if not failures:
        raise HTTPException(400, "Nothing to repair - run a validation first.")
    if session.pending_llm is not None and not session.pending_llm.task.done():
        raise HTTPException(409, "Another request is already running for this plan.")
    task = asyncio.create_task(
        asyncio.to_thread(
            _repair_plan, session.provider, session.steps, failures,
            session.last_component_failures,
        )
    )
    session.pending_llm = PendingLLM(task=task, note="Repaired from Salesforce errors")
    return {"started": True}


@app.get("/api/plan/repair/status")
async def plan_repair_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    pending = session.pending_llm
    if pending is None:
        raise HTTPException(400, "No repair in progress - call /api/plan/repair/start first.")
    if not pending.task.done():
        return waiting(session.provider)
    session.pending_llm = None
    session.steps = llm_result(pending.task)
    # Bumping the version (not touching approved_version) is what makes
    # session.approved false again - the same mechanism Session.record()
    # relies on for the single-Flow path, not a separate reset here.
    session.version += 1
    session.apply_policy()
    return {"done": True, **plan_view(session_id, session)}


def _revise_plan_step(
    provider: Provider, steps: List[StepResult], step_name: str, instruction: str
) -> List[StepResult]:
    """Re-run the one named step with a proactive change request - the
    "before validating" counterpart to _repair_plan's "after it's rejected"."""
    return [
        refine_step(provider, step, instruction) if step.step.name == step_name else step
        for step in steps
    ]


@app.post("/api/plan/step/revise/start")
async def plan_step_revise_start(body: PlanStepReviseRequest) -> Dict[str, Any]:
    """Ask the model to change one step, before anything has been validated."""
    session = get_plan_session(body.session_id)
    if not body.instruction.strip():
        raise HTTPException(400, "Say what should change.")
    if not any(s.step.name == body.step_name for s in session.steps):
        raise HTTPException(404, f"No such step: {body.step_name!r}")
    if session.pending_llm is not None and not session.pending_llm.task.done():
        raise HTTPException(409, "Another request is already running for this plan.")
    task = asyncio.create_task(
        asyncio.to_thread(
            _revise_plan_step, session.provider, session.steps, body.step_name, body.instruction,
        )
    )
    session.pending_llm = PendingLLM(
        task=task, note=f"Revised {body.step_name}: {body.instruction}"
    )
    return {"started": True}


@app.get("/api/plan/step/revise/status")
async def plan_step_revise_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    pending = session.pending_llm
    if pending is None:
        raise HTTPException(
            400, "No revision in progress - call /api/plan/step/revise/start first."
        )
    if not pending.task.done():
        return waiting(session.provider)
    session.pending_llm = None
    session.steps = llm_result(pending.task)
    session.version += 1
    session.apply_policy()
    return {"done": True, **plan_view(session_id, session)}


@app.post("/api/plan/revise/start")
async def plan_revise_start(body: PlanReviseRequest) -> Dict[str, Any]:
    """
    Ask the model to change the plan itself - add, remove or restructure
    steps - not just one step's content (that's
    /api/plan/step/revise/start). Every step is regenerated against the
    revised plan; see revise_plan's docstring in planner.py for why a partial
    re-run isn't safe here.
    """
    session = get_plan_session(body.session_id)
    if not body.instruction.strip():
        raise HTTPException(400, "Say what should change about the plan.")
    if session.pending_llm is not None and not session.pending_llm.task.done():
        raise HTTPException(409, "Another request is already running for this plan.")
    task = asyncio.create_task(
        asyncio.to_thread(
            revise_plan, session.provider, session.plan, session.planner_messages,
            body.instruction,
        )
    )
    session.pending_llm = PendingLLM(task=task, note=f"Revised plan: {body.instruction}")
    return {"started": True}


@app.get("/api/plan/revise/status")
async def plan_revise_status(session_id: str) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    pending = session.pending_llm
    if pending is None:
        raise HTTPException(
            400, "No plan revision in progress - call /api/plan/revise/start first."
        )
    if not pending.task.done():
        return waiting(session.provider)
    session.pending_llm = None
    revision = llm_result(pending.task)
    session.plan = revision.plan
    session.planner_messages = revision.messages
    session.steps = revision.steps
    session.version += 1
    session.apply_policy()
    return {"done": True, **plan_view(session_id, session)}


@app.get("/api/plan/session/{session_id}")
def plan_session_view(session_id: str) -> Dict[str, Any]:
    """Re-fetch a plan session's current state - same reasoning as
    /api/session/{session_id} above."""
    session = get_plan_session(session_id)
    return plan_view(session_id, session)


def _zip_response(zip_bytes: bytes, filename: str) -> Response:
    return Response(
        zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _plan_package_files(
    session: PlanSession,
) -> tuple[Dict[str, str], Dict[str, List[str]]]:
    """
    The plan's member files and package.xml types, org-free - shared by the
    zip download and Push to Git.

    A Permission Set that merges into an existing one needs that org's
    current copy to merge with, so it is left out; a new one is included,
    since it needs nothing from an org.
    """
    files, types = _bundle_files_and_types(session.steps, session.reference, session.plan.agent_user)
    config = session.permission_set_config
    if config is not None and config.mode == "new":
        grant = build_grant_from_steps(
            session.steps, config.label or config.api_name or "Generated Access",
        )
        if session.reference:
            grant.description = tag_description(grant.description, session.reference)
        files[f"permissionsets/{config.api_name}.permissionset"] = generate_permission_set(
            grant, api_name=config.api_name,
        )
        types.setdefault("PermissionSet", []).append(config.api_name)
    return files, types


@app.get("/api/plan/session/{session_id}/package")
def plan_package(session_id: str) -> Response:
    """
    The plan as a Metadata API zip (package.xml + every component), for
    deploying with `sf project deploy start --metadata-dir`, Workbench, or
    anything else that takes a package - without going through this tool's
    own org connection. Needs no org and no approval: it deploys nothing.
    """
    session = get_plan_session(session_id)
    files, types = _plan_package_files(session)
    zip_bytes = build_deploy_package(files, types, session.api_version)
    return _zip_response(zip_bytes, f"package-{session_id}.zip")


class PlanBundleRequest(BaseModel):
    # The current XML of the existing Permission Set this plan merges into, read from
    # the org by the extension. Only needed when the plan's grant mode is "existing".
    existing_permission_set_xml: Optional[str] = None


def _plan_components(session: "PlanSession", config: Optional[PermissionSetConfig]) -> List[Dict[str, Any]]:
    """What the extension needs to build Setup links after a deploy (see _plan_component_urls)."""
    out: List[Dict[str, Any]] = []
    for r in session.steps:
        v = r.value
        for cls, kind in ((CustomObject, "object"), (CustomField, "field"), (ApexClass, "apex"),
                          (Flow, "flow"), (LightningComponent, "lwc")):
            if isinstance(v, cls):
                item = {"type": kind, "name": v.api_name, "step": r.step.name}
                if kind == "field":
                    item["object"] = v.object_api_name
                out.append(item)
                break
    if config is not None and config.api_name:
        out.append({"type": "permission_set", "name": config.api_name, "step": "Permission Set"})
    return out


def _plan_bundle(session_id: str, existing_xml: Optional[str]) -> Dict[str, Any]:
    session = get_plan_session(session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the plan before fetching its bundle.")
    _require_agent_user(session)
    files, types = _bundle_files_and_types(session.steps, session.reference, session.plan.agent_user)
    config = session.permission_set_config
    pending_ps = None
    if config is not None:
        if config.mode == "existing" and existing_xml is None:
            # Left out, and named, so the extension can fetch it and POST this route again.
            if _permission_set_grant(config, session.steps, session.reference) is not None:
                pending_ps = config.api_name
        else:
            _add_permission_set_xml(config, session.steps, existing_xml, files, types, session.reference)
    return {
        "version": session.version, "files": files, "types": types,
        "api_version": _deploy_api_version(session),
        "needs_existing_permission_set": pending_ps,
        "kind": "plan",
        "name": None,
        "components": _plan_components(session, config),
        "prompt_templates": [r.value.api_name for r in session.steps if isinstance(r.value, PromptTemplate)],
    }


@app.get("/api/plan/session/{session_id}/bundle")
def plan_bundle(session_id: str) -> Dict[str, Any]:
    """The approved plan's deploy bundle (see /api/session/{id}/bundle). When the plan merges
    into an existing Permission Set, that file is omitted and `needs_existing_permission_set`
    names it: POST the same path with `existing_permission_set_xml` to get the merged bundle."""
    return _plan_bundle(session_id, None)


@app.post("/api/plan/session/{session_id}/bundle")
def plan_bundle_with_context(session_id: str, body: PlanBundleRequest) -> Dict[str, Any]:
    return _plan_bundle(session_id, body.existing_permission_set_xml)


@app.post("/api/plan/session/{session_id}/deploy-result")
def plan_deploy_result(session_id: str, body: DeployResultRequest) -> Dict[str, Any]:
    """Records a validate/deploy the extension ran, for the repair loop and the agent-publish gate."""
    session = get_plan_session(session_id)
    session.last_failures = [] if body.success else list(body.failures)
    session.last_component_failures = [] if body.success else [
        ComponentProblem(
            full_name=c.get("full_name", ""), problem_type=c.get("problem_type", "Error"),
            problem=c.get("problem", ""),
        )
        for c in body.component_failures
    ]
    deployed = body.deployed if body.deployed is not None else not body.check_only
    if body.success and deployed:
        session.deployed_version = body.version if body.version is not None else session.version
    return {"recorded": True, "failures": session.last_failures, "plan": plan_view(session_id, session)}


class GitPushRequest(BaseModel):
    session_id: str
    token: str
    repo: str
    base: Optional[str] = None
    folder: Optional[str] = None
    branch: Optional[str] = None
    title: Optional[str] = None


def _plan_pr_body(session: PlanSession, folder: str) -> str:
    lines = []
    if session.reference:
        lines.append(f"Reference: **{session.reference}**\n")
    if session.plan.reasoning:
        lines.append(session.plan.reasoning + "\n")
    lines.append("### Components")
    for result in session.steps:
        description = getattr(result.value, "description", None)
        suffix = f" - {description}" if description else ""
        lines.append(f"- **{result.step.artifact_type}** `{result.step.name}`{suffix}")
    if session.plan.how_to_test:
        lines.append("\n### How to test\n" + session.plan.how_to_test)
    where = folder or "."
    lines.append(
        "\n### Deploying\n"
        f"The Metadata API package is in `{where}/` (package.xml + components):\n"
        f"```\nsf project deploy start --metadata-dir {where}\n```"
    )
    config = session.permission_set_config
    if config is not None and config.mode != "new":
        lines.append(
            "\n_The Permission Set grant merges into an existing set, which needs that "
            "org's current copy - it is not included here._"
        )
    lines.append("\nGenerated by sfdc-flow-forge.")
    return "\n".join(lines)


@app.post("/api/plan/git/push")
async def plan_git_push(body: GitPushRequest) -> Dict[str, Any]:
    """
    Commit the plan's Metadata API package to a new branch of a GitHub repo and
    open a pull request. Deploys nothing to Salesforce, so - like the zip
    download - it needs no approval. The token is used for this call only.
    """
    session = get_plan_session(body.session_id)
    try:
        repo = github.clean_repo(body.repo)
        ref = session.reference
        folder = github.clean_folder(
            body.folder if body.folder is not None else f"sfdc-flow-forge/{body.session_id[:8]}"
        )
        branch = github.clean_branch(
            body.branch or f"flow-forge/{ref + '-' if ref else ''}{body.session_id[:8]}"
        )
        files, types = _plan_package_files(session)
        files["package.xml"] = _deploy_package_xml(types, session.api_version)
        title = (body.title or "").strip() or (
            f"{ref}: " if ref else ""
        ) + f"sfdc-flow-forge plan ({len(session.steps)} step(s))"
        return await github.open_pull_request(
            token=body.token, repo=repo, base=body.base, branch=branch, files=files,
            folder=folder, commit_message=title, title=title,
            body=_plan_pr_body(session, folder),
        )
    except github.GitHubError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach GitHub: {type(exc).__name__}") from exc


class ReferenceRequest(BaseModel):
    kind: Literal["session", "plan"]
    session_id: str
    reference: Optional[str] = None


@app.post("/api/reference")
def set_reference(body: ReferenceRequest) -> Dict[str, Any]:
    """Set or clear the optional external reference (e.g. a JIRA key). It only
    affects what gets deployed or exported next - the reviewed IR is left
    alone - so it needs no re-approval and does not bump the version."""
    try:
        reference = clean_reference(body.reference)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.kind == "plan":
        get_plan_session(body.session_id).reference = reference
    else:
        get_session(body.session_id).reference = reference
    return {"reference": reference}


@app.get("/api/plan/session/{session_id}/report")
def plan_report(session_id: str) -> PlainTextResponse:
    """
    A standalone HTML document of everything in this plan, downloadable
    before deploying anything - so what's pending approval can be handed to
    a person or another system for review, outside this tool entirely.
    Print-to-PDF from a browser covers the "I need a PDF" case without this
    project taking on a PDF-rendering dependency. Uses the same
    render_standalone_report mcp_server.py's build/revise tools render their
    report_html fragment from, so the web UI and an MCP client never
    describe the same plan two different ways.
    """
    session = get_plan_session(session_id)
    status = "approved" if session.approved else "not yet approved"
    report = render_standalone_report(
        session.steps,
        title=f"Plan report - v{session.version}",
        meta=f"{len(session.steps)} step(s) - {status}"
        + (f" - Ref: {session.reference}" if session.reference else ""),
        plan=session.plan,
        scenarios=(
            session.scenarios if session.scenarios_version == session.version else None
        ),
    )
    return PlainTextResponse(
        report, media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="plan-{session_id}.html"'},
    )


# --------------------------------------------------------------------------
# Share ("Share this") - snapshot a Session or PlanSession, hand back a link
# + password, let anyone with both resume from that point within 24h.
# --------------------------------------------------------------------------
#
# Snapshotting stores only what's needed to rebuild a Session/PlanSession
# that looks exactly like the one shared - the generated IR of every
# artifact plus the LLM conversation that produced it (so a resumed session
# can still refine()/repair() with real history, not a blank slate) - never
# the provider/API key, which the resuming browser supplies itself, same as
# opening any fresh design. A resumed session/plan always lands with
# approved_version=0 (not yet approved), whatever the original's approval
# state was - the whole point is "pick up review from here", not silently
# inheriting someone else's approval.

# kind -> (IR model class, generator class) for the single-artifact Session
# path - mirrors _APEX_LIKE plus flow/lwc, kept separate since this also
# needs FlowGenerator/LwcGenerator which _APEX_LIKE's two-entry dict does not.
_SESSION_KIND_TYPES: Dict[str, tuple] = {
    "flow": (Flow, FlowGenerator),
    "apex": (ApexClass, ApexClassGenerator),
    "trigger": (ApexTrigger, ApexTriggerGenerator),
    "lwc": (LightningComponent, LwcGenerator),
}

# artifact_type -> IR model class for a plan step - the reverse of
# planner.py's _GENERATOR_BY_TYPE, needed here to rebuild a StepResult's
# `value` from plain JSON via model_validate.
_PLAN_STEP_IR_CLASS: Dict[str, Any] = {
    "flow": Flow,
    "object": CustomObject,
    "field": CustomField,
    "apex": ApexClass,
    "lwc": LightningComponent,
    "mdt": MetadataType,
    "mdt_record": CustomMetadataRecord,
    "platform_event": PlatformEvent,
    "prompt_template": PromptTemplate,
    "agent": Agent,
}


def _messages_to_json(messages: List[Message]) -> List[Dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _messages_from_json(data: List[Dict[str, str]]) -> List[Message]:
    return [Message(role=m["role"], content=m["content"]) for m in data]


def _session_snapshot(session: Session) -> Dict[str, Any]:
    return {
        "type": "session",
        "kind": session.kind,
        "api_version": session.api_version,
        "activate": session.activate,
        "history": session.history,
        "imported": session.imported,
        "reference": session.reference,
        "repairs": session.result.repairs,
        "messages": _messages_to_json(session.result.messages),
        "ir": session.artifact.model_dump(mode="json"),
    }


def _session_from_snapshot(
    data: Dict[str, Any], provider: Provider, note: str = "Resumed from a shared link",
) -> Session:
    kind = data["kind"]
    ir_cls, generator_cls = _SESSION_KIND_TYPES[kind]
    artifact = ir_cls.model_validate(data["ir"])
    messages = _messages_from_json(data["messages"])
    result: Any
    if kind == "flow":
        result = GenerationResult(flow=artifact, messages=messages, repairs=data["repairs"])
    else:
        result = IRGenerationResult(value=artifact, messages=messages, repairs=data["repairs"])
    session = Session(
        generator=generator_cls(provider),
        result=result,
        kind=kind,
        activate=data["activate"],
        api_version=data["api_version"],
        history=list(data["history"]) + [{"note": note, "version": "1"}],
        imported=data["imported"],
        reference=data.get("reference"),
    )
    session.apply_policy()
    return session


def _plan_snapshot(session: "PlanSession") -> Dict[str, Any]:
    config = session.permission_set_config
    return {
        "type": "plan",
        "api_version": session.api_version,
        "reference": session.reference,
        "permission_set_config": (
            None if config is None
            else {"mode": config.mode, "label": config.label, "api_name": config.api_name}
        ),
        "steps": [
            {
                "step": r.step.model_dump(mode="json"),
                "repairs": r.repairs,
                "messages": _messages_to_json(r.messages),
                "ir": r.value.model_dump(mode="json"),
            }
            for r in session.steps
        ],
    }


def _plan_from_snapshot(data: Dict[str, Any], provider: Provider) -> "PlanSession":
    steps: List[StepResult] = []
    for entry in data["steps"]:
        step = PlanStep.model_validate(entry["step"])
        ir_cls = _PLAN_STEP_IR_CLASS[step.artifact_type]
        value = ir_cls.model_validate(entry["ir"])
        steps.append(StepResult(
            step=step, value=value, repairs=entry["repairs"],
            messages=_messages_from_json(entry["messages"]),
        ))

    config_data = data.get("permission_set_config")
    permission_set_config = (
        None if config_data is None else PermissionSetConfig(**config_data)
    )

    session = PlanSession(
        provider=provider,
        plan=Plan(steps=[r.step for r in steps]),
        steps=steps,
        api_version=data["api_version"],
        permission_set_config=permission_set_config,
        reference=data.get("reference"),
    )
    session.apply_policy()
    return session


class ShareRequest(BaseModel):
    kind: Literal["session", "plan"]
    session_id: str


class ShareUnlockRequest(BaseModel):
    password: str
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_key: Optional[str] = None


@app.post("/api/share/start")
async def share_start(body: ShareRequest) -> Dict[str, Any]:
    if body.kind == "plan":
        snapshot = _plan_snapshot(get_plan_session(body.session_id))
    else:
        snapshot = _session_snapshot(get_session(body.session_id))

    token = share.generate_token()
    password = share.generate_password()
    try:
        await share.create_share(token, password, snapshot)
    except share.ShareError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"token": token, "password": password, "expires_in": share.SHARE_TTL_SECONDS}


@app.post("/api/share/{token}/unlock")
async def share_unlock(token: str, body: ShareUnlockRequest) -> Dict[str, Any]:
    try:
        snapshot = await share.consume_password(token, body.password)
    except share.ShareError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    if snapshot["type"] == "plan":
        plan_session = _plan_from_snapshot(snapshot, provider)
        session_id = uuid.uuid4().hex
        PLAN_SESSIONS[session_id] = plan_session
        return {"done": True, "share_kind": "plan", **plan_view(session_id, plan_session)}

    session = _session_from_snapshot(snapshot, provider)
    session_id = uuid.uuid4().hex
    SESSIONS[session_id] = session
    return {"done": True, "share_kind": "session", **view(session_id, session)}


# --------------------------------------------------------------------------
# Save / restore a session as a local file (.sfdcbuildertool.json). Same
# snapshot as Share, but the file stays with the person - nothing is stored
# server-side and nothing expires. A restored session always lands unapproved,
# for the same reason a shared one does.
# --------------------------------------------------------------------------


class SaveRequest(BaseModel):
    kind: Literal["session", "plan"]
    session_id: str
    name: Optional[str] = Field(default=None, max_length=80)


class RestoreRequest(BaseModel):
    file: Dict[str, Any]
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_key: Optional[str] = None


@app.post("/api/save")
def save_session(body: SaveRequest) -> Dict[str, Any]:
    if body.kind == "plan":
        plan_session = get_plan_session(body.session_id)
        snapshot = _plan_snapshot(plan_session)
        title = plan_session.steps[0].step.name if plan_session.steps else None
        version = None
    else:
        session = get_session(body.session_id)
        snapshot = _session_snapshot(session)
        title = session.artifact.api_name
        version = session.version
    return {
        "filename": savefile.filename(
            reference=snapshot.get("reference"), name=body.name, kind=body.kind,
            title=title, version=version,
        ),
        "file": savefile.wrap(snapshot, name=body.name, title=title),
    }


@app.post("/api/restore")
def restore_session(body: RestoreRequest) -> Dict[str, Any]:
    try:
        snapshot = savefile.unwrap(body.file)
        # The file is hand-editable and the reference ends up in deployed XML.
        snapshot["reference"] = clean_reference(snapshot.get("reference"))
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
        note = "Restored from a saved session file"
        if snapshot["type"] == "plan":
            plan_session = _plan_from_snapshot(snapshot, provider)
            session_id = uuid.uuid4().hex
            PLAN_SESSIONS[session_id] = plan_session
            return {"done": True, "restored_kind": "plan", **plan_view(session_id, plan_session)}
        session = _session_from_snapshot(snapshot, provider, note=note)
        session_id = uuid.uuid4().hex
        SESSIONS[session_id] = session
        return {"done": True, "restored_kind": "session", **view(session_id, session)}
    except savefile.SaveFileError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        # pydantic's ValidationError is a ValueError.
        raise HTTPException(400, "The session file is damaged or incomplete.") from exc


INSTALL_DIR = STATIC / "install"


@app.get("/install/sfdc-flow-forge-installer.ps1")
def install_windows() -> PlainTextResponse:
    """Downloadable Windows installer for the MCP server - see the "Install
    MCP" link under Options. Served with an explicit filename/attachment
    header since StaticFiles doesn't set Content-Disposition and browsers
    have no reliable registered MIME type for .ps1."""
    return PlainTextResponse(
        (INSTALL_DIR / "sfdc-flow-forge-installer.ps1").read_text(),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="sfdc-flow-forge-installer.ps1"'},
    )


@app.get("/install/sfdc-flow-forge-installer.sh")
def install_macos() -> PlainTextResponse:
    """Downloadable macOS/Linux installer for the MCP server - see install_windows."""
    return PlainTextResponse(
        (INSTALL_DIR / "sfdc-flow-forge-installer.sh").read_text(),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="sfdc-flow-forge-installer.sh"'},
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy() -> HTMLResponse:
    return HTMLResponse(privacy.page())


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# The portal builder ("New Agentforce" tab) lives in its own module and borrows
# this file's credentials() so it authenticates exactly like every other route.
from flowtool.portal_api import make_router as _portal_router  # noqa: E402

app.include_router(_portal_router(credentials))

app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="SFDC Flow Tool web UI")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
    )
    print(f"SFDC Flow Tool on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
