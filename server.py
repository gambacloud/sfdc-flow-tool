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
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flowtool.config import load_env
from flowtool.ir import Flow
from flowtool.llm import (
    AnthropicProvider,
    FlowGenerator,
    GenerationResult,
    LLMError,
    Provider,
)
from flowtool.llm import GeminiProvider
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.sfdc import (
    RetrieveError,
    flow_builder_url,
    list_flows,
    retrieve_flow,
    validate_flow,
)
from flowtool.xmlgen import generate as generate_xml

ROOT = Path(__file__).parent
load_env(ROOT)

# Lives inside the flowtool package (rather than beside server.py) so it ships
# as package data when this repo is installed as a dependency elsewhere.
STATIC = ROOT / "flowtool" / "static"

app = FastAPI(title="SFDC Flow Tool")

PROVIDERS = {"anthropic": AnthropicProvider, "gemini": GeminiProvider}

PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
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
    /status polls it - the task itself is the same validate_flow() call this
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
    generator: FlowGenerator
    result: GenerationResult
    activate: bool = False
    api_version: str = "62.0"
    # Bumped on every change; deploy compares the two.
    version: int = 1
    approved_version: int = 0
    history: List[Dict[str, str]] = field(default_factory=list)
    # What the org said last time, kept so a repair does not depend on the
    # browser sending error text back to the server.
    last_failures: List[str] = field(default_factory=list)
    # True when the flow came out of the org rather than from a description.
    imported: bool = False
    pending_deploy: Optional[PendingDeploy] = None
    pending_llm: Optional[PendingLLM] = None
    pending_explain: Optional["asyncio.Task"] = None

    @property
    def flow(self) -> Flow:
        return self.result.flow

    @property
    def approved(self) -> bool:
        return self.approved_version == self.version

    def record(self, result: GenerationResult, note: str) -> None:
        self.result = result
        self.version += 1
        self.history.append({"note": note, "version": str(self.version)})
        self.apply_policy()

    def apply_policy(self) -> None:
        """Status and API version are the tool's call, never the model's."""
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


def get_session(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session. Start a new design.")
    return session


def view(session_id: str, session: Session) -> Dict[str, Any]:
    flow = session.flow
    return {
        "session_id": session_id,
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
                f"put GEMINI_API_KEY or ANTHROPIC_API_KEY in a {ROOT / '.env'} "
                "file and restart the server."
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
    effort: str = "high"
    activate: bool = False
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


class DeployRequest(OrgRequest):
    confirm: bool = False


class ImportRequest(BaseModel):
    api_name: str
    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: str = "high"
    api_version: str = "62.0"
    api_key: Optional[str] = None


class ExplainRequest(BaseModel):
    session_id: str
    question: Optional[str] = None


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
        # Same Connected App and same env var name as salesforce-debugtool, so
        # a Heroku deployment that already sets one for that app needs nothing
        # new here. Empty means the login buttons stay hidden.
        "clientId": os.environ.get("SF_CLIENT_ID", ""),
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
        provider = build_provider(body.provider, None, "high", body.api_key)
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
        return {"done": False}
    del DESIGN_JOBS[job_id]

    try:
        result = pending.task.result()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

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


@app.get("/api/flows")
async def flows(
    org: Optional[str] = None,
    instance_url: Optional[str] = None,
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    url, token = credentials(org, instance_url, access_token)
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


@app.post("/api/import/start")
async def import_start(body: ImportRequest) -> Dict[str, Any]:
    """
    Kick off a retrieve in the background and hand back a job id. A retrieve
    can run long enough to cross Heroku's 30s request limit, so the browser
    polls /api/import/status instead of waiting on one request.
    """
    url, token = credentials(body.org, body.instance_url, body.access_token)
    task = asyncio.create_task(
        retrieve_flow(url, token, body.api_name, api_version=body.api_version)
    )
    job_id = uuid.uuid4().hex
    IMPORTS[job_id] = PendingImport(task=task, instance_url=url, request=body)
    return {"job_id": job_id}


@app.get("/api/import/status")
async def import_status(job_id: str) -> Dict[str, Any]:
    """
    Pull a flow out of the org and adopt it, so the next refinement edits it
    rather than designing a replacement from its description.
    """
    pending = IMPORTS.get(job_id)
    if pending is None:
        raise HTTPException(404, "Unknown import job.")
    if not pending.task.done():
        return {"done": False}
    del IMPORTS[job_id]

    try:
        xml = pending.task.result()
    except (RetrieveError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc

    body = pending.request
    try:
        flow = parse_flow(xml, api_name=body.api_name)
    except UnsupportedFlow as exc:
        # Refusing is the point: a diagram missing the parts we cannot model
        # would describe a different flow than the one in the org.
        raise HTTPException(422, str(exc)) from exc

    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    generator = FlowGenerator(provider)
    session_id = uuid.uuid4().hex
    session = Session(
        generator=generator,
        result=generator.adopt(
            flow, f"the flow {body.api_name} from {pending.instance_url}"
        ),
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
    session.pending_explain = asyncio.create_task(
        asyncio.to_thread(session.generator.explain, session.flow, body.question)
    )
    return {"started": True}


@app.get("/api/explain/status")
async def explain_status(session_id: str) -> Dict[str, Any]:
    session = get_session(session_id)
    task = session.pending_explain
    if task is None:
        raise HTTPException(400, "No explain in progress - call /api/explain/start first.")
    if not task.done():
        return {"done": False}
    session.pending_explain = None
    try:
        explanation = task.result()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"done": True, "explanation": explanation}


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
        return {"done": False}
    session.pending_llm = None
    try:
        result = pending.task.result()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    # flow changed since the browser rendered it, the numbers no longer match.
    if body.version != session.version:
        raise HTTPException(
            409, "The flow changed since you looked at it. Review it again."
        )
    session.approved_version = session.version
    return view(body.session_id, session)


def _start_deploy(
    session: Session,
    org: Optional[str],
    instance_url: Optional[str],
    access_token: Optional[str],
    check_only: bool,
) -> None:
    if session.pending_deploy is not None and not session.pending_deploy.task.done():
        raise HTTPException(409, "A validate/deploy is already running for this flow.")
    url, token = credentials(org, instance_url, access_token)
    task = asyncio.create_task(
        validate_flow(
            url,
            token,
            session.flow.api_name,
            generate_xml(session.flow),
            api_version=session.flow.api_version,
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
        link = await flow_builder_url(
            pending.instance_url, pending.token, session.flow.api_name,
            api_version=session.flow.api_version,
        )
    return {
        "done": True,
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "instance_url": pending.instance_url,
        "flow_url": link,
    }


@app.get("/api/session/{session_id}/{artifact}")
def artifact(session_id: str, artifact: str) -> PlainTextResponse:
    session = get_session(session_id)
    flow = session.flow
    bodies = {
        "xml": (generate_xml(flow), "application/xml"),
        "markdown": (to_markdown(flow), "text/markdown"),
        "ir": (flow.model_dump_json(exclude_none=True, indent=2), "application/json"),
    }
    if artifact not in bodies:
        raise HTTPException(404, f"No such artifact: {artifact}")
    text, media_type = bodies[artifact]
    return PlainTextResponse(text, media_type=media_type)


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
