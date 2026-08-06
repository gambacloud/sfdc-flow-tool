"""
HTTP wrapper around the pipeline in forge.py.

The approval gate is enforced here, not in the browser. Every change to a flow
bumps its version, and validate and deploy refuse to run unless the version the
user approved is still the current one. A client-side check would be decoration.

    python server.py     ->  http://localhost:8000
"""

from __future__ import annotations

import argparse
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


def credentials(org_alias: Optional[str]) -> tuple[str, str]:
    """Only the sf CLI path is exposed over HTTP - no token ever crosses it."""
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


class DeployRequest(OrgRequest):
    confirm: bool = False


class ImportRequest(BaseModel):
    api_name: str
    org: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: str = "high"
    api_version: str = "62.0"
    api_key: Optional[str] = None


class ExplainRequest(BaseModel):
    session_id: str
    question: Optional[str] = None


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
    }


@app.post("/api/design")
def design(body: DesignRequest) -> Dict[str, Any]:
    if not body.request.strip():
        raise HTTPException(400, "Describe what the flow should do.")
    try:
        provider = build_provider(body.provider, body.model, body.effort, body.api_key)
        generator = FlowGenerator(provider)
        result = generator.generate(body.request)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc

    session_id = uuid.uuid4().hex
    session = Session(
        generator=generator,
        result=result,
        activate=body.activate,
        api_version=body.api_version,
        history=[{"note": body.request, "version": "1"}],
    )
    session.apply_policy()
    SESSIONS[session_id] = session
    return view(session_id, session)


@app.get("/api/flows")
async def flows(org: Optional[str] = None) -> Dict[str, Any]:
    instance_url, token = credentials(org)
    try:
        found = await list_flows(instance_url, token)
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


@app.post("/api/import")
async def import_flow(body: ImportRequest) -> Dict[str, Any]:
    """
    Pull a flow out of the org and adopt it, so the next refinement edits it
    rather than designing a replacement from its description.
    """
    instance_url, token = credentials(body.org)
    try:
        xml = await retrieve_flow(
            instance_url, token, body.api_name, api_version=body.api_version
        )
    except (RetrieveError, TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc

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
        result=generator.adopt(flow, f"the flow {body.api_name} from {instance_url}"),
        # An imported flow keeps the status it already has in the org, so
        # opening one to read it cannot quietly propose deactivating it.
        activate=flow.status == "Active",
        api_version=flow.api_version,
        history=[{"note": f"Imported {body.api_name} from the org", "version": "1"}],
        imported=True,
    )
    session.apply_policy()
    SESSIONS[session_id] = session
    return view(session_id, session)


@app.post("/api/explain")
def explain(body: ExplainRequest) -> Dict[str, str]:
    session = get_session(body.session_id)
    try:
        return {"explanation": session.generator.explain(session.flow, body.question)}
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/refine")
def refine(body: RefineRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not body.instruction.strip():
        raise HTTPException(400, "Say what should change.")
    try:
        result = session.generator.refine(session.result, body.instruction)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.record(result, body.instruction)
    return view(body.session_id, session)


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


@app.post("/api/validate")
async def validate(body: OrgRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the flow before validating it.")

    instance_url, token = credentials(body.org)
    result = await validate_flow(
        instance_url,
        token,
        session.flow.api_name,
        generate_xml(session.flow),
        api_version=session.flow.api_version,
        check_only=True,
    )
    failures = [str(failure) for failure in result.failures]
    if not failures and result.error_message:
        failures = [result.error_message]
    session.last_failures = failures
    return {
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "checked_version": session.version,
    }


@app.post("/api/repair")
def repair(body: OrgRequest) -> Dict[str, Any]:
    """Feed the last validation failures back to the model."""
    session = get_session(body.session_id)
    failures = session.last_failures
    if not failures:
        raise HTTPException(400, "Nothing to repair - run a validation first.")
    try:
        result = session.generator.repair_from_salesforce(session.result, failures)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The flow changed, so the previous approval no longer covers it.
    session.record(result, "Repaired from Salesforce errors")
    return view(body.session_id, session)


@app.post("/api/deploy")
async def deploy(body: DeployRequest) -> Dict[str, Any]:
    session = get_session(body.session_id)
    if not session.approved:
        raise HTTPException(403, "Approve the flow before deploying it.")
    if not body.confirm:
        raise HTTPException(400, "Deploying needs an explicit confirmation.")

    instance_url, token = credentials(body.org)
    result = await validate_flow(
        instance_url,
        token,
        session.flow.api_name,
        generate_xml(session.flow),
        api_version=session.flow.api_version,
        check_only=False,
    )
    failures = [str(failure) for failure in result.failures]
    if not failures and result.error_message:
        failures = [result.error_message]
    link = await flow_builder_url(
        instance_url, token, session.flow.api_name,
        api_version=session.flow.api_version,
    )
    return {
        "success": result.success,
        "status": result.status,
        "failures": failures,
        "instance_url": instance_url,
        "flow_url": link if result.success else None,
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
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


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
