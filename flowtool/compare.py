"""
"Compare with the org": for each step of a plan, is it new, or does it already exist - and if it
exists, what would deploying change?

Read-only. Nothing here writes to the org. A step whose state cannot be read (a missing
permission, a type the org does not expose) is reported as "unknown" with the reason; a failed
look-up never stops the others, and never blocks a deploy.

The result is a list of plain dicts (one per step) so the browser can show it as is:
    {"step": "<step name>", "status": "new" | "exists" | "unknown",
     "summary": "<one line>", "changes": ["label: 'A' -> 'B'", ...], "diff": "<unified diff or ''>"}
"""

from __future__ import annotations

import asyncio
import difflib
from typing import Any, Dict, List, Optional

import httpx

from .ir import Flow
from .ir_agent import Agent
from .ir_apex import ApexClass
from .ir_lwc import LightningComponent
from .ir_mdt import CustomMetadataRecord, MetadataType
from .ir_object import CustomField, CustomObject
from .ir_platform_event import PlatformEvent
from .ir_prompt_template import PromptTemplate

_DIFF_LIMIT = 6000  # characters of diff text sent to the browser per step

# What the describe call calls each field type of the IR.
_DESCRIBE_TYPE = {
    "Text": {"string"},
    "TextArea": {"textarea"},
    "LongTextArea": {"textarea"},
    "Number": {"double", "int", "currency", "percent"},
    "Checkbox": {"boolean"},
    "Picklist": {"picklist"},
    "Lookup": {"reference"},
    "MasterDetail": {"reference"},
}


class NotFound(Exception):
    """The org answered that the thing does not exist."""


class OrgReader:
    """The few read-only REST calls the comparison needs."""

    def __init__(self, instance_url: str, token: str, api_version: str = "64.0"):
        base = instance_url if "://" in instance_url else f"https://{instance_url}"
        self.base = base.rstrip("/")
        self.api_version = api_version
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client: Optional[httpx.AsyncClient] = None
        self._describes: Dict[str, "asyncio.Future[Optional[Dict[str, Any]]]"] = {}

    async def __aenter__(self) -> "OrgReader":
        self._client = httpx.AsyncClient(timeout=60.0, headers=self._headers)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base}/services/data/v{self.api_version}/{path}"

    async def query(self, soql: str, tooling: bool = False) -> List[Dict[str, Any]]:
        path = "tooling/query" if tooling else "query"
        resp = await self._client.get(self._url(path), params={"q": soql})
        if resp.status_code != 200:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json().get("records", [])

    async def describe(self, sobject: str) -> Optional[Dict[str, Any]]:
        """The object's describe, or None when it does not exist. Cached per object."""
        if sobject not in self._describes:
            self._describes[sobject] = asyncio.ensure_future(self._describe(sobject))
        return await self._describes[sobject]

    async def _describe(self, sobject: str) -> Optional[Dict[str, Any]]:
        resp = await self._client.get(self._url(f"sobjects/{sobject}/describe"))
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _diff(old: str, new: str, name: str) -> str:
    lines = list(difflib.unified_diff(
        old.replace("\r\n", "\n").splitlines(), new.replace("\r\n", "\n").splitlines(),
        fromfile=f"{name} (org)", tofile=f"{name} (this plan)", lineterm="", n=2,
    ))
    text = "\n".join(lines)
    return text if len(text) <= _DIFF_LIMIT else text[:_DIFF_LIMIT] + "\n... (diff shortened)"


def _result(step: str, status: str, summary: str, changes: Optional[List[str]] = None, diff: str = "") -> Dict[str, Any]:
    return {"step": step, "status": status, "summary": summary, "changes": changes or [], "diff": diff}


def _new(step: str, what: str) -> Dict[str, Any]:
    return _result(step, "new", f"New - {what} does not exist in the org yet.")


def _same_or_changed(step: str, what: str, changes: List[str], diff: str = "") -> Dict[str, Any]:
    if not changes and not diff:
        return _result(step, "exists", f"Exists - {what} matches what this plan would deploy.")
    return _result(step, "exists", f"Exists - deploying replaces {what}.", changes, diff)


async def _compare_apex(org: OrgReader, step: str, value: ApexClass) -> Dict[str, Any]:
    rows = await org.query(f"SELECT Body FROM ApexClass WHERE Name = '{_q(value.api_name)}'")
    if not rows:
        return _new(step, f"class {value.api_name}")
    old = rows[0].get("Body") or ""
    same = old.replace("\r\n", "\n").strip() == value.body.replace("\r\n", "\n").strip()
    return _same_or_changed(step, f"class {value.api_name}", [] if same else ["source differs"],
                            "" if same else _diff(old, value.body, value.api_name))


async def _compare_flow(org: OrgReader, step: str, value: Flow) -> Dict[str, Any]:
    rows = await org.query(
        "SELECT VersionNumber, Status FROM Flow WHERE Definition.DeveloperName = "
        f"'{_q(value.api_name)}' ORDER BY VersionNumber DESC LIMIT 1", tooling=True,
    )
    if not rows:
        return _new(step, f"flow {value.api_name}")
    latest = rows[0]
    return _result(
        step, "exists",
        f"Exists - flow {value.api_name} is at version {latest.get('VersionNumber')} ({latest.get('Status')}); "
        f"deploying adds version {int(latest.get('VersionNumber') or 0) + 1}.",
    )


async def _compare_object(org: OrgReader, step: str, value: CustomObject) -> Dict[str, Any]:
    described = await org.describe(value.api_name)
    if described is None:
        return _new(step, f"object {value.api_name}")
    changes = []
    if described.get("label") != value.label:
        changes.append(f"label: {described.get('label')!r} -> {value.label!r}")
    if described.get("labelPlural") != value.plural_label:
        changes.append(f"plural label: {described.get('labelPlural')!r} -> {value.plural_label!r}")
    return _same_or_changed(step, f"object {value.api_name}", changes)


async def _compare_field(org: OrgReader, step: str, value: CustomField) -> Dict[str, Any]:
    described = await org.describe(value.object_api_name)
    if described is None:
        return _result(step, "new", f"New - {value.api_name} (its object {value.object_api_name} is not in the org yet).")
    existing = next((f for f in described.get("fields", []) if f.get("name", "").lower() == value.api_name.lower()), None)
    if existing is None:
        return _new(step, f"field {value.object_api_name}.{value.api_name}")
    changes: List[str] = []
    if existing.get("label") != value.label:
        changes.append(f"label: {existing.get('label')!r} -> {value.label!r}")
    if value.type != "Formula":
        allowed = _DESCRIBE_TYPE.get(value.type, set())
        if allowed and existing.get("type") not in allowed:
            changes.append(f"type: {existing.get('type')} -> {value.type}")
    if value.type == "Text" and value.length and existing.get("length") != value.length:
        changes.append(f"length: {existing.get('length')} -> {value.length}")
    if value.type == "Number":
        if value.precision is not None and existing.get("precision") != value.precision:
            changes.append(f"precision: {existing.get('precision')} -> {value.precision}")
        if value.scale is not None and existing.get("scale") != value.scale:
            changes.append(f"scale: {existing.get('scale')} -> {value.scale}")
    if value.type == "Picklist":
        have = [p.get("value") for p in existing.get("picklistValues", []) if p.get("active", True)]
        if set(have) != set(value.picklist_values):
            gone = [v for v in have if v not in value.picklist_values]
            added = [v for v in value.picklist_values if v not in have]
            if added:
                changes.append("picklist values added: " + ", ".join(added))
            if gone:
                changes.append("picklist values dropped: " + ", ".join(gone))
    if value.type in ("Lookup", "MasterDetail") and value.reference_to:
        if value.reference_to not in (existing.get("referenceTo") or []):
            changes.append(f"points to: {', '.join(existing.get('referenceTo') or []) or '?'} -> {value.reference_to}")
    return _same_or_changed(step, f"field {value.object_api_name}.{value.api_name}", changes)


async def _compare_lwc(org: OrgReader, step: str, value: LightningComponent) -> Dict[str, Any]:
    rows = await org.query(
        f"SELECT Id FROM LightningComponentBundle WHERE DeveloperName = '{_q(value.api_name)}'", tooling=True,
    )
    if not rows:
        return _new(step, f"component {value.api_name}")
    return _result(step, "exists", f"Exists - deploying replaces component {value.api_name}.")


async def _compare_event(org: OrgReader, step: str, value: PlatformEvent) -> Dict[str, Any]:
    described = await org.describe(value.api_name)
    if described is None:
        return _new(step, f"platform event {value.api_name}")
    return _result(step, "exists", f"Exists - deploying replaces platform event {value.api_name}.")


async def _compare_mdt(org: OrgReader, step: str, value: MetadataType) -> Dict[str, Any]:
    described = await org.describe(value.api_name)
    if described is None:
        return _new(step, f"custom metadata type {value.api_name}")
    return _result(step, "exists", f"Exists - deploying replaces custom metadata type {value.api_name}.")


async def _compare_mdt_record(org: OrgReader, step: str, value: CustomMetadataRecord) -> Dict[str, Any]:
    type_name = value.type_api_name if value.type_api_name.endswith("__mdt") else value.type_api_name + "__mdt"
    if await org.describe(type_name) is None:
        return _result(step, "new", f"New - its type {type_name} is not in the org yet.")
    rows = await org.query(f"SELECT DeveloperName FROM {type_name} WHERE DeveloperName = '{_q(value.developer_name)}'")
    if not rows:
        return _new(step, f"record {value.developer_name}")
    return _result(step, "exists", f"Exists - deploying replaces record {value.developer_name}.")


async def _compare_agent(org: OrgReader, step: str, value: Agent) -> Dict[str, Any]:
    rows = await org.query(
        f"SELECT Id, AgentType FROM BotDefinition WHERE DeveloperName = '{_q(value.api_name)}'",
    )
    if not rows:
        return _new(step, f"agent {value.api_name}")
    return _result(
        step, "exists",
        f"Exists - agent {value.api_name} is published ({rows[0].get('AgentType')}); deploying updates its draft, "
        "publishing adds a new version.",
    )


async def _compare_prompt(meta, step: str, value: PromptTemplate) -> Dict[str, Any]:
    from .prompt_import import list_prompt_templates, retrieve_prompt_template

    names = {t["api_name"] for t in await list_prompt_templates(meta)}
    if value.api_name not in names:
        return _new(step, f"prompt template {value.api_name}")
    existing = await retrieve_prompt_template(meta, value.api_name)
    changes: List[str] = []
    if existing.response_format != value.response_format:
        changes.append(f"answer format: {existing.response_format} -> {value.response_format}")
    old_inputs = sorted(i.name for i in existing.inputs)
    new_inputs = sorted(i.name for i in value.inputs)
    if old_inputs != new_inputs:
        changes.append(f"inputs: {', '.join(old_inputs) or 'none'} -> {', '.join(new_inputs) or 'none'}")
    same_text = existing.content.strip() == value.content.strip()
    if not same_text:
        changes.append("prompt text differs")
    return _same_or_changed(step, f"prompt template {value.api_name}", changes,
                            "" if same_text else _diff(existing.content, value.content, value.api_name))


_COMPARERS = {
    ApexClass: _compare_apex,
    Flow: _compare_flow,
    CustomObject: _compare_object,
    CustomField: _compare_field,
    LightningComponent: _compare_lwc,
    PlatformEvent: _compare_event,
    MetadataType: _compare_mdt,
    CustomMetadataRecord: _compare_mdt_record,
    Agent: _compare_agent,
}


async def compare_steps(org: OrgReader, steps, meta=None) -> List[Dict[str, Any]]:
    """One comparison per step, in the plan's order. `steps` are StepResult-like objects
    (`.step.name`, `.value`). `meta` is a MetadataClient, needed only for prompt templates."""
    gate = asyncio.Semaphore(4)

    async def one(result) -> Dict[str, Any]:
        name = result.step.name
        value = result.value
        async with gate:
            try:
                if isinstance(value, PromptTemplate):
                    if meta is None:
                        return _result(name, "unknown", "Not compared - no metadata connection.")
                    return await _compare_prompt(meta, name, value)
                comparer = _COMPARERS.get(type(value))
                if comparer is None:
                    return _result(name, "unknown", "Not compared - this kind of step is not checked yet.")
                return await comparer(org, name, value)
            except Exception as exc:  # noqa: BLE001 - one failed look-up must not hide the others
                return _result(name, "unknown", f"Could not compare: {str(exc)[:200]}")

    return list(await asyncio.gather(*(one(r) for r in steps)))
