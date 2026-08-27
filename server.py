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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flowtool.config import load_env
from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass, ApexTrigger
from flowtool.ir_lwc import LightningComponent
from flowtool.ir_object import CustomField, CustomObject
from flowtool.llm import (
    AnthropicProvider,
    ApexClassGenerator,
    ApexTriggerGenerator,
    FlowGenerator,
    GenerationResult,
    IRGenerationResult,
    KB_CHAT_PROMPT,
    LLMError,
    LwcGenerator,
    Message,
    Provider,
)
from flowtool.llm import GeminiProvider, OllamaProvider
from flowtool.mermaid import element_index, to_markdown, to_mermaid, to_test_guide
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.planner import (
    Plan, PlanStep, PlannerGenerator, StepResult, execute_plan, refine_step, repair_step,
)
from flowtool.report import render_standalone_report
from flowtool.sfdc import (
    ORG_SUMMARY_TYPE_GROUPS,
    RetrieveError,
    component_setup_url,
    list_apex_classes,
    list_apex_triggers,
    list_flows,
    list_lwc_components,
    retrieve_all_flows,
    retrieve_apex_class,
    retrieve_apex_trigger,
    retrieve_flow,
    retrieve_lwc_component,
    retrieve_org_summary_zip,
    validate_bundle,
)
from flowtool.xmlgen import generate as generate_xml
from flowtool.xmlgen_apex import generate_apex, generate_apex_trigger
from flowtool.xmlgen_lwc import generate_lwc
from flowtool.xmlgen_object import generate_field_delta, generate_object
from survey import Survey, text_report

ROOT = Path(__file__).parent
load_env(ROOT)

# Lives inside the flowtool package (rather than beside server.py) so it ships
# as package data when this repo is installed as a dependency elsewhere.
STATIC = ROOT / "flowtool" / "static"

app = FastAPI(title="SFDC Flow Tool")

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
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider, "ollama": OllamaProvider}

PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
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
    api_version: str = "62.0"
    # Bumped on every change; deploy compares the two.
    version: int = 1
    approved_version: int = 0
    history: List[Dict[str, str]] = field(default_factory=list)
    # What the org said last time, kept so a repair does not depend on the
    # browser sending error text back to the server.
    last_failures: List[str] = field(default_factory=list)
    # True when the artifact came out of the org rather than from a description.
    imported: bool = False
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
        name = found[0]
    if name not in PROVIDERS:
        raise LLMError(f"Unknown provider {name!r}.")
    options: Dict[str, Any] = {"effort": effort}
    if model:
        options["model"] = model
    if api_key:
        options["api_key"] = api_key
    return PROVIDERS[name](**options)


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
    api_version: str = "62.0"
    api_key: Optional[str] = None


class LwcDesignRequest(BaseModel):
    """Same shape as DesignRequest, minus `activate` - an LWC has no Draft/
    Active concept the way a Flow does."""

    request: str
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "62.0"
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
    kind: Literal["flow", "apex", "trigger", "lwc"] = "flow"
    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "62.0"
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
        "default_provider": providers[0] if providers else None,
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
    else:
        task = asyncio.create_task(
            retrieve_flow(url, token, body.api_name, api_version=body.api_version)
        )
    job_id = uuid.uuid4().hex
    IMPORTS[job_id] = PendingImport(task=task, instance_url=url, request=body)
    return {"job_id": job_id}


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

    session_id = uuid.uuid4().hex

    if body.kind in _APEX_LIKE:
        model_cls, generator_cls, noun = _APEX_LIKE[body.kind]
        artifact = model_cls(
            api_name=body.api_name, body=source, api_version=body.api_version,
        )
        generator = generator_cls(provider)
        session = Session(
            generator=generator,
            result=generator.adopt(
                artifact, f"the {noun} {body.api_name} from {pending.instance_url}"
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
                component, f"the Lightning Web Component {body.api_name} from {pending.instance_url}"
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
                flow, f"the flow {body.api_name} from {pending.instance_url}"
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
        apex = session.apex
        body, meta = generate_apex(apex)
        return (
            {
                f"classes/{apex.api_name}.cls": body,
                f"classes/{apex.api_name}.cls-meta.xml": meta,
            },
            {"ApexClass": [apex.api_name]},
        )
    if session.kind == "trigger":
        trigger = session.trigger
        body, meta = generate_apex_trigger(trigger)
        return (
            {
                f"triggers/{trigger.api_name}.trigger": body,
                f"triggers/{trigger.api_name}.trigger-meta.xml": meta,
            },
            {"ApexTrigger": [trigger.api_name]},
        )
    if session.kind == "lwc":
        component = session.lwc
        return generate_lwc(component), {"LightningComponentBundle": [component.api_name]}
    flow = session.flow
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


@app.get("/api/session/{session_id}/{artifact}")
def artifact(session_id: str, artifact: str) -> PlainTextResponse:
    session = get_session(session_id)

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
                meta=f"{len(component.body.splitlines())} line(s) - {status}",
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
                meta=f"{len(component.js.splitlines())} line(s) of js - {status}",
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
            meta=f"{len(flow.elements)} element(s) - {status}",
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
    effort: Literal["medium", "high"] = "medium"
    api_version: str = "62.0"
    api_key: Optional[str] = None


class PlanExecuteRequest(BaseModel):
    plan_id: str
    # Off by default - see execute_plan's docstring for why. Exposed as a UI
    # toggle so it can still be compared against, not removed outright.
    parallel: bool = False


class PlanStepReviseRequest(BaseModel):
    session_id: str
    step_name: str
    instruction: str


@dataclass
class PendingPlan:
    """A planning call running in the background - same shape as PendingDesign,
    one level up: this produces a Plan, not yet any generated metadata."""

    task: "asyncio.Task"
    provider: Provider
    api_version: str


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
    api_version: str


PLANS: Dict[str, StoredPlan] = {}


@dataclass
class PendingPlanExecution:
    task: "asyncio.Task"
    provider: Provider
    plan: Plan
    api_version: str


PLAN_EXECUTIONS: Dict[str, PendingPlanExecution] = {}


@dataclass
class PlanSession:
    provider: Provider
    plan: Plan
    steps: List[StepResult]
    activate: bool = False
    api_version: str = "62.0"
    version: int = 1
    approved_version: int = 0
    pending_deploy: Optional[PendingDeploy] = None
    pending_llm: Optional[PendingLLM] = None
    # What the org said last time validate failed. `last_failures` is the
    # human-readable form the UI shows; `last_component_failures` keeps each
    # failure's own component name (ComponentProblem.full_name) alongside it,
    # so a repair can route each failure to the step it actually names
    # instead of re-generating the whole plan - see _repair_plan.
    last_failures: List[str] = field(default_factory=list)
    last_component_failures: List[Any] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.approved_version == self.version

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
        "depends_on": result.step.depends_on,
        "repairs": result.repairs,
    }
    if isinstance(value, Flow):
        entry.update({
            "api_name": value.api_name,
            "label": value.label,
            "element_count": len(value.elements),
            "mermaid": to_mermaid(value),
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
        })
    return entry


def plan_view(session_id: str, session: PlanSession) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "version": session.version,
        "approved": session.approved,
        "steps": [_step_view(r) for r in session.steps],
        "usage": session.provider.usage.as_dict(),
    }


def _bundle_files_and_types(
    steps: List[StepResult],
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
    other_steps: List[StepResult] = []

    for result in steps:
        value = result.value
        if isinstance(value, CustomObject):
            objects[value.api_name] = value
        elif isinstance(value, CustomField):
            fields_by_object.setdefault(value.object_api_name, []).append(value)
        else:
            other_steps.append(result)

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

    for result in other_steps:
        value = result.value
        if isinstance(value, Flow):
            files[f"flows/{value.api_name}.flow"] = generate_xml(value)
            types.setdefault("Flow", []).append(value.api_name)
        elif isinstance(value, ApexClass):
            body, meta = generate_apex(value)
            files[f"classes/{value.api_name}.cls"] = body
            files[f"classes/{value.api_name}.cls-meta.xml"] = meta
            types.setdefault("ApexClass", []).append(value.api_name)
        elif isinstance(value, LightningComponent):
            files.update(generate_lwc(value))
            types.setdefault("LightningComponentBundle", []).append(value.api_name)

    return files, types


@app.post("/api/plan/start")
async def plan_start(body: PlanRequest) -> Dict[str, Any]:
    """Kick off planning in the background - same reasoning as design_start:
    the provider call is synchronous, so it runs in a thread."""
    if not body.request.strip():
        raise HTTPException(400, "Describe what should be built.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    generator = PlannerGenerator(provider)
    task = asyncio.create_task(asyncio.to_thread(generator.generate, body.request))
    job_id = uuid.uuid4().hex
    PLAN_JOBS[job_id] = PendingPlan(task=task, provider=provider, api_version=body.api_version)
    return {"job_id": job_id}


@app.get("/api/plan/status")
async def plan_status(job_id: str) -> Dict[str, Any]:
    pending = PLAN_JOBS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown plan job.")
    if not pending.task.done():
        return waiting(pending.provider)
    del PLAN_JOBS[job_id]

    result = llm_result(pending.task)
    plan_id = uuid.uuid4().hex
    PLANS[plan_id] = StoredPlan(
        provider=pending.provider, plan=result.value, api_version=pending.api_version
    )
    steps = [
        {"name": s.name, "artifact_type": s.artifact_type, "brief": s.brief,
         "depends_on": s.depends_on}
        for s in result.value.steps
    ]
    return {"done": True, "plan_id": plan_id, "steps": steps}


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
        task=task, provider=stored.provider, plan=stored.plan, api_version=stored.api_version,
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
    )
    session.apply_policy()
    PLAN_SESSIONS[session_id] = session
    return {"done": True, **plan_view(session_id, session)}


@app.post("/api/plan/approve")
def plan_approve(body: ApproveRequest) -> Dict[str, Any]:
    session = get_plan_session(body.session_id)
    if body.version != session.version:
        raise HTTPException(
            409, "The plan changed since you looked at it. Review it again."
        )
    session.approved_version = session.version
    return plan_view(body.session_id, session)


def _start_plan_deploy(
    session: PlanSession,
    org: Optional[str],
    instance_url: Optional[str],
    access_token: Optional[str],
    check_only: bool,
) -> None:
    if session.pending_deploy is not None and not session.pending_deploy.task.done():
        raise HTTPException(409, "A validate/deploy is already running for this plan.")
    url, token = credentials(org, instance_url, access_token)
    files, types = _bundle_files_and_types(session.steps)
    task = asyncio.create_task(
        validate_bundle(
            url, token, files, types,
            api_version=session.api_version, check_only=check_only,
        )
    )
    session.pending_deploy = PendingDeploy(task=task, instance_url=url, token=token)


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
        urls = await _plan_component_urls(session, pending.instance_url, pending.token)
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": _failures(result),
        "instance_url": pending.instance_url,
        "setup_urls": urls,
    }


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


@app.get("/api/plan/session/{session_id}")
def plan_session_view(session_id: str) -> Dict[str, Any]:
    """Re-fetch a plan session's current state - same reasoning as
    /api/session/{session_id} above."""
    session = get_plan_session(session_id)
    return plan_view(session_id, session)


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
        meta=f"{len(session.steps)} step(s) - {status}",
    )
    return PlainTextResponse(
        report, media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="plan-{session_id}.html"'},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


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
