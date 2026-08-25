"""
Salesforce Metadata API client — validate (checkOnly) and deploy a Flow.

Ported from the sibling sfdc-deploy-tool, which already proved this path works
without the sf CLI: session id + instance url, straight at /services/Soap/m/.
"""

from __future__ import annotations

import asyncio
import base64
import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
SOAP_NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "met": METADATA_NS,
}
_HEADERS = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'}


@dataclass
class ComponentProblem:
    full_name: str
    problem: str
    problem_type: str
    line: Optional[str] = None
    column: Optional[str] = None

    def __str__(self) -> str:
        where = f" (line {self.line}, col {self.column})" if self.line else ""
        return f"[{self.problem_type}] {self.full_name}{where}: {self.problem}"


@dataclass
class DeployResult:
    id: str
    status: str
    success: bool
    failures: List[ComponentProblem] = field(default_factory=list)
    error_message: Optional[str] = None


def _fault_string(payload: str) -> Optional[str]:
    """Pull the human-readable message out of a SOAP fault, if this is one."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    node = root.find(".//faultstring")
    if node is None or not node.text:
        return None
    message = node.text.strip()
    if "INVALID_SESSION_ID" in message:
        return f"{message}\nThe session id is expired or belongs to a different org."
    return message


def _deploy_package_xml(types: Dict[str, List[str]], api_version: str) -> str:
    """
    The <Package> manifest for a deploy zip: one <types> block per metadata
    type, each member of that type listed inside. A plain document (no SOAP
    envelope, no met: prefix) - the deploy zip's package.xml, unlike
    build_multi_type_package's <met:unpackaged> fragment, which is inlined
    straight into a retrieve request body instead of zipped.
    """
    blocks = "".join(
        "    <types>\n"
        + "".join(f"        <members>{m}</members>\n" for m in members)
        + f"        <name>{type_name}</name>\n    </types>\n"
        for type_name, members in types.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Package xmlns="{METADATA_NS}">\n'
        f"{blocks}"
        f"    <version>{api_version}</version>\n"
        "</Package>\n"
    )


def build_deploy_package(
    files: Dict[str, str], types: Dict[str, List[str]], api_version: str
) -> bytes:
    """
    Metadata-API-format ZIP spanning one or more component types: package.xml
    plus arbitrary member files. Generalizes the single-type case below so one
    deploy can bundle a Flow with the Object/Field/Apex components it depends
    on - the same package.xml shape verify_object_apex.py (a dev-QA script)
    built by hand before this became the real, shipped version.

    `files` maps each member's zip path to its content (`"objects/X__c.object"`,
    `"classes/Y.cls"`, ...). `types` maps each metadata type name to the
    members package.xml should declare for it - a CustomField's member name is
    the dotted `"Object__c.Field__c"` form Salesforce expects, not just the
    field's own name.
    """
    package_xml = _deploy_package_xml(types, api_version)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package.xml", package_xml)
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def build_package(flow_api_name: str, flow_xml: str, api_version: str) -> bytes:
    """Metadata-API-format ZIP: package.xml + flows/<name>.flow."""
    return build_deploy_package(
        {f"flows/{flow_api_name}.flow": flow_xml},
        {"Flow": [flow_api_name]},
        api_version,
    )


def _normalise(instance_url: str) -> str:
    """
    The SOAP endpoints only answer on the API host. Lightning and Experience
    hosts 302 to a login page instead, which is confusing to debug, so map the
    common ones over rather than letting the redirect happen.
    """
    url = instance_url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    # Keep only the origin — a pasted URL often carries /lightning/page/home.
    parts = httpx.URL(url)
    host = parts.host
    for suffix in (".lightning.force.com", ".my.site.com", ".force.com"):
        if host.endswith(suffix):
            host = host[: -len(suffix)] + ".my.salesforce.com"
            break
    return str(httpx.URL(scheme=parts.scheme or "https", host=host, port=parts.port))


class MetadataClient:
    def __init__(self, instance_url: str, session_id: str, api_version: str = "62.0"):
        self.endpoint = f"{_normalise(instance_url)}/services/Soap/m/{api_version}"
        self.session_id = session_id
        self.api_version = api_version
        self._client = httpx.AsyncClient(timeout=120.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MetadataClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    def _envelope(self, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            f' xmlns:met="{METADATA_NS}">'
            "<soapenv:Header><met:SessionHeader>"
            f"<met:sessionId>{self.session_id}</met:sessionId>"
            "</met:SessionHeader></soapenv:Header>"
            f"<soapenv:Body>{body}</soapenv:Body>"
            "</soapenv:Envelope>"
        )

    async def _post(self, body: str) -> ET.Element:
        resp = await self._client.post(
            self.endpoint, content=self._envelope(body).encode("utf-8"), headers=_HEADERS
        )

        if resp.is_redirect:
            location = resp.headers.get("location", "(no Location header)")
            raise RuntimeError(
                f"{self.endpoint} redirected ({resp.status_code}) to {location}.\n"
                "The Metadata API only answers on the API host. Use the instance URL "
                "shown in Setup > Company Information as 'My Domain' "
                "(https://<your-domain>.my.salesforce.com), not the Lightning URL."
            )

        if resp.status_code != 200:
            # SOAP faults come back as 500 with a readable faultstring.
            fault = _fault_string(resp.text)
            if fault:
                raise RuntimeError(f"Salesforce rejected the call: {fault}")
            raise RuntimeError(
                f"Metadata API call failed ({resp.status_code}): {resp.text[:600]}"
            )

        return ET.fromstring(resp.text)

    async def start_deploy(self, zip_bytes: bytes, check_only: bool = True) -> str:
        zip_b64 = base64.b64encode(zip_bytes).decode("ascii")
        body = (
            "<met:deploy>"
            f"<met:zipFile>{zip_b64}</met:zipFile>"
            "<met:DeployOptions>"
            "<met:allowMissingFiles>false</met:allowMissingFiles>"
            "<met:autoUpdatePackage>false</met:autoUpdatePackage>"
            f"<met:checkOnly>{str(check_only).lower()}</met:checkOnly>"
            "<met:ignoreWarnings>false</met:ignoreWarnings>"
            "<met:performRetrieve>false</met:performRetrieve>"
            "<met:purgeOnDelete>false</met:purgeOnDelete>"
            "<met:rollbackOnError>true</met:rollbackOnError>"
            "<met:singlePackage>true</met:singlePackage>"
            "<met:testLevel>NoTestRun</met:testLevel>"
            "</met:DeployOptions>"
            "</met:deploy>"
        )
        root = await self._post(body)
        node = root.find(".//met:deployResponse/met:result/met:id", SOAP_NS)
        if node is None or not node.text:
            raise RuntimeError("deploy() returned no job id")
        return node.text

    async def start_retrieve(self, flow_api_name: str) -> str:
        body = (
            "<met:retrieve><met:retrieveRequest>"
            f"<met:apiVersion>{self.api_version}</met:apiVersion>"
            "<met:singlePackage>true</met:singlePackage>"
            f"{build_retrieve_package(flow_api_name, self.api_version)}"
            "</met:retrieveRequest></met:retrieve>"
        )
        root = await self._post(body)
        node = root.find(".//met:retrieveResponse/met:result/met:id", SOAP_NS)
        if node is None or not node.text:
            raise RetrieveError("retrieve() returned no job id")
        return node.text

    async def start_retrieve_types(self, types: Dict[str, List[str]]) -> str:
        """Same as start_retrieve, for a manifest spanning more than one type."""
        body = (
            "<met:retrieve><met:retrieveRequest>"
            f"<met:apiVersion>{self.api_version}</met:apiVersion>"
            "<met:singlePackage>true</met:singlePackage>"
            f"{build_multi_type_package(types, self.api_version)}"
            "</met:retrieveRequest></met:retrieve>"
        )
        root = await self._post(body)
        node = root.find(".//met:retrieveResponse/met:result/met:id", SOAP_NS)
        if node is None or not node.text:
            raise RetrieveError("retrieve() returned no job id")
        return node.text

    async def list_metadata(self, type_name: str) -> List[Dict[str, Optional[str]]]:
        """
        Every component of one type the org will admit to, each with its
        namespace and manageableState - the only way to tell a component
        that lives in the org's own metadata from one that came in through a
        managed package before asking to retrieve it. A bare `*` retrieve
        does not distinguish the two; this does.
        """
        body = (
            "<met:listMetadata>"
            f"<met:queries><met:type>{type_name}</met:type></met:queries>"
            f"<met:asOfVersion>{self.api_version}</met:asOfVersion>"
            "</met:listMetadata>"
        )
        root = await self._post(body)
        return [
            {
                "full_name": result.findtext("met:fullName", None, SOAP_NS),
                "namespace_prefix": result.findtext("met:namespacePrefix", None, SOAP_NS),
                "manageable_state": result.findtext("met:manageableState", None, SOAP_NS),
            }
            for result in root.findall(".//met:listMetadataResponse/met:result", SOAP_NS)
        ]

    async def wait_for_retrieve(
        self, job_id: str, poll_seconds: float = 1.5, timeout_seconds: float = 120.0
    ) -> bytes:
        body_template = (
            "<met:checkRetrieveStatus>"
            f"<met:asyncProcessId>{job_id}</met:asyncProcessId>"
            "<met:includeZip>true</met:includeZip>"
            "</met:checkRetrieveStatus>"
        )
        waited = 0.0
        while waited < timeout_seconds:
            root = await self._post(body_template)
            result = root.find(".//met:checkRetrieveStatusResponse/met:result", SOAP_NS)
            if result is None:
                raise RetrieveError("checkRetrieveStatus returned no result")

            done = (result.findtext("met:done", "", SOAP_NS) or "").lower() == "true"
            if done:
                status = result.findtext("met:status", "", SOAP_NS)
                if status not in ("Succeeded", "SucceededPartial"):
                    message = result.findtext("met:errorMessage", "", SOAP_NS)
                    raise RetrieveError(f"Retrieve {status}: {message or 'no detail'}")
                encoded = result.findtext("met:zipFile", "", SOAP_NS)
                if not encoded:
                    raise RetrieveError("Retrieve returned no package")
                return base64.b64decode(encoded)

            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
        raise TimeoutError(f"retrieve {job_id} still running after {timeout_seconds}s")

    async def check_status(self, job_id: str) -> DeployResult:
        body = (
            "<met:checkDeployStatus>"
            f"<met:asyncProcessId>{job_id}</met:asyncProcessId>"
            "<met:includeDetails>true</met:includeDetails>"
            "</met:checkDeployStatus>"
        )
        root = await self._post(body)
        result = root.find(".//met:checkDeployStatusResponse/met:result", SOAP_NS)
        if result is None:
            raise RuntimeError("checkDeployStatus returned no result")

        def text(path: str) -> Optional[str]:
            node = result.find(path, SOAP_NS)
            return node.text if node is not None else None

        failures = []
        for failure in result.findall(".//met:componentFailures", SOAP_NS):
            def ftext(tag: str) -> Optional[str]:
                node = failure.find(f"met:{tag}", SOAP_NS)
                return node.text if node is not None else None

            failures.append(
                ComponentProblem(
                    full_name=ftext("fullName") or "?",
                    problem=ftext("problem") or "?",
                    problem_type=ftext("problemType") or "Error",
                    line=ftext("lineNumber"),
                    column=ftext("columnNumber"),
                )
            )

        return DeployResult(
            id=text("met:id") or job_id,
            status=text("met:status") or "Unknown",
            success=(text("met:success") or "false").lower() == "true",
            failures=failures,
            error_message=text("met:errorMessage"),
        )

    async def deploy_and_wait(
        self, zip_bytes: bytes, check_only: bool = True, poll_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ) -> DeployResult:
        job_id = await self.start_deploy(zip_bytes, check_only=check_only)
        waited = 0.0
        while waited < timeout_seconds:
            result = await self.check_status(job_id)
            if result.status in {"Succeeded", "Failed", "Canceled", "SucceededPartial"}:
                return result
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
        raise TimeoutError(f"deploy {job_id} still running after {timeout_seconds}s")


@dataclass
class FlowSummary:
    api_name: str
    label: str
    active: bool
    description: Optional[str] = None
    last_modified: Optional[str] = None


@dataclass
class ApexClassSummary:
    api_name: str
    last_modified: Optional[str] = None


class RetrieveError(RuntimeError):
    pass


async def list_flows(
    instance_url: str, session_id: str, api_version: str = "62.0"
) -> List[FlowSummary]:
    """
    Every flow definition in the org, newest first.

    FlowDefinition rather than Flow: a flow has one row per version, and what a
    user picks from a list is the flow, not version 7 of it.
    """
    base = _normalise(instance_url)
    query = (
        "SELECT DeveloperName, MasterLabel, Description, ActiveVersionId, "
        "LastModifiedDate FROM FlowDefinition ORDER BY LastModifiedDate DESC"
    )
    url = f"{base}/services/data/v{api_version}/tooling/query"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            url,
            params={"q": query},
            headers={"Authorization": f"Bearer {session_id}"},
        )
    if resp.status_code != 200:
        fault = _fault_string(resp.text)
        raise RetrieveError(fault or f"Could not list flows ({resp.status_code}): {resp.text[:300]}")

    return [
        FlowSummary(
            api_name=record["DeveloperName"],
            label=record.get("MasterLabel") or record["DeveloperName"],
            active=bool(record.get("ActiveVersionId")),
            description=record.get("Description"),
            last_modified=record.get("LastModifiedDate"),
        )
        for record in resp.json().get("records", [])
    ]


async def flow_builder_url(
    instance_url: str, session_id: str, flow_api_name: str, api_version: str = "62.0"
) -> str:
    """
    A link straight into Flow Builder for the version that was just deployed.

    Needs the version id, which only exists after the deploy, so this is
    resolved afterwards rather than constructed from the API name. Falls back to
    the Flows list, which is still one click from the flow.
    """
    base = _normalise(instance_url)
    fallback = f"{base}/lightning/setup/Flows/home"

    query = (
        "SELECT Id FROM Flow WHERE Definition.DeveloperName = "
        f"'{flow_api_name}' ORDER BY VersionNumber DESC LIMIT 1"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base}/services/data/v{api_version}/tooling/query",
                params={"q": query},
                headers={"Authorization": f"Bearer {session_id}"},
            )
        if resp.status_code != 200:
            return fallback
        records = resp.json().get("records", [])
        if not records:
            return fallback
        version_id = records[0]["Id"]
    except (httpx.HTTPError, ValueError, KeyError):
        # A missing link is a nuisance; a failed deploy report is not. Never let
        # this step turn a successful deploy into an error.
        return fallback

    return f"{base}/builder_platform_interaction/flowBuilder.app?flowId={version_id}"


async def component_setup_url(
    instance_url: str, session_id: str, artifact_type: str, api_name: str,
    api_version: str = "62.0", object_api_name: Optional[str] = None,
) -> str:
    """
    A direct Setup link for a component that was just deployed, so a person
    doesn't have to go hunting for what a plan actually created. Object and
    field links are addressable by name alone; Apex needs its Id resolved
    the same way flow_builder_url resolves a flow version's, since Setup's
    class editor is keyed by Id, not by name.
    """
    base = _normalise(instance_url)
    if artifact_type == "object":
        return f"{base}/lightning/setup/ObjectManager/{api_name}/Details/view"
    if artifact_type == "field":
        return (
            f"{base}/lightning/setup/ObjectManager/{object_api_name}"
            f"/FieldsAndRelationships/{api_name}/view"
        )
    if artifact_type == "flow":
        return await flow_builder_url(instance_url, session_id, api_name, api_version)
    if artifact_type == "apex":
        fallback = f"{base}/lightning/setup/ApexClasses/home"
        query = f"SELECT Id FROM ApexClass WHERE Name = '{api_name}'"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{base}/services/data/v{api_version}/tooling/query",
                    params={"q": query},
                    headers={"Authorization": f"Bearer {session_id}"},
                )
            if resp.status_code != 200:
                return fallback
            records = resp.json().get("records", [])
            if not records:
                return fallback
            class_id = records[0]["Id"]
        except (httpx.HTTPError, ValueError, KeyError):
            return fallback
        return f"{base}/lightning/setup/ApexClasses/page?address=%2F{class_id}"
    return f"{base}/lightning/setup/SetupOneHome/home"


async def retrieve_all_flows(
    instance_url: str, session_id: str, api_version: str = "62.0"
) -> Dict[str, str]:
    """
    Every flow in the org, in one retrieve.

    A retrieve is a job: submit, poll, download. Doing that per flow turns a
    survey of fifty flows into fifty round trips, so this asks for `*` once and
    unpacks the result.
    """
    async with MetadataClient(instance_url, session_id, api_version) as client:
        job_id = await client.start_retrieve("*")
        zip_bytes = await client.wait_for_retrieve(job_id, timeout_seconds=600.0)

    flows: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if not name.startswith("flows/") or not name.endswith(".flow"):
                continue
            api_name = name[len("flows/"): -len(".flow")]
            flows[api_name] = archive.read(name).decode("utf-8")
    return flows


def build_retrieve_package(flow_api_name: str, api_version: str) -> str:
    return (
        f'<met:unpackaged><met:types><met:members>{flow_api_name}</met:members>'
        f"<met:name>Flow</met:name></met:types>"
        f"<met:version>{api_version}</met:version></met:unpackaged>"
    )


def build_multi_type_package(types: Dict[str, List[str]], api_version: str) -> str:
    """The same manifest shape as build_retrieve_package, for several types at once."""
    blocks = "".join(
        f"<met:types>{''.join(f'<met:members>{m}</met:members>' for m in members)}"
        f"<met:name>{type_name}</met:name></met:types>"
        for type_name, members in types.items()
    )
    return (
        f"<met:unpackaged>{blocks}<met:version>{api_version}</met:version>"
        "</met:unpackaged>"
    )


# The checkbox list the UI offers, in order - grouped by what a user
# actually thinks in (not one checkbox per raw Metadata API type, which
# would be a wall of nearly-identical options for Apex and for components).
# `default` is what a fresh retrieve uses when the browser sends no explicit
# choice. Profile is real and available, but not on by default - field-level
# security alone made up 35% of a real org's knowledge base, more than any
# other single group.
ORG_SUMMARY_TYPE_GROUPS: List[Dict[str, Any]] = [
    {"group": "objects", "label": "Custom Objects", "types": ["CustomObject"], "default": True},
    {"group": "apex", "label": "Apex (classes & triggers)",
     "types": ["ApexClass", "ApexTrigger"], "default": True},
    {"group": "flows", "label": "Flows", "types": ["Flow"], "default": True},
    {"group": "components", "label": "Lightning components (LWC & Aura)",
     "types": ["LightningComponentBundle", "AuraDefinitionBundle"], "default": True},
    {"group": "profiles",
     "label": "Profiles (large - field-level security repeats per field, per profile)",
     "types": ["Profile"], "default": False},
    {"group": "custom_metadata", "label": "Custom Metadata records",
     "types": ["CustomMetadata"], "default": True},
]

_ORG_SUMMARY_GROUP_TYPES: Dict[str, List[str]] = {
    g["group"]: g["types"] for g in ORG_SUMMARY_TYPE_GROUPS
}
_DEFAULT_ORG_SUMMARY_GROUPS: List[str] = [
    g["group"] for g in ORG_SUMMARY_TYPE_GROUPS if g["default"]
]


def _is_unmanaged(entry: Dict[str, Optional[str]]) -> bool:
    """
    A component belongs to the org itself, not a package, when it carries no
    namespace and its manageableState is not "installed" - the value a
    subscriber org's own listMetadata sets on anything that arrived through
    a managed package. An unlocked package still under active development on
    the source org ("beta"/"deprecatedEditable"/"installedEditable") is
    excluded too, rather than guessed about.
    """
    if entry.get("namespace_prefix"):
        return False
    return (entry.get("manageable_state") or "unmanaged") == "unmanaged"


async def _unmanaged_org_summary_types(
    client: "MetadataClient", types: List[str]
) -> Dict[str, List[str]]:
    """
    Each requested type's own "everything" retrieve would include components
    that arrived through an installed managed package (a sample app, an
    AppExchange package), which is usually most of the bulk and none of it
    something this tool's user actually built or can act on. listMetadata
    gives each component's namespace and manageableState before anything is
    retrieved, so those can be left out of the manifest instead of filtered
    out of a knowledge base after paying to generate and read it.
    """
    results = await asyncio.gather(*(client.list_metadata(t) for t in types))
    manifest: Dict[str, List[str]] = {}
    for type_name, entries in zip(types, results):
        names = [e["full_name"] for e in entries if e.get("full_name") and _is_unmanaged(e)]
        if names:
            manifest[type_name] = names
    return manifest


async def retrieve_org_summary_zip(
    instance_url: str, session_id: str, api_version: str = "62.0",
    groups: Optional[List[str]] = None,
) -> bytes:
    """
    The requested checkbox groups (ORG_SUMMARY_TYPE_GROUPS's own defaults if
    the browser sent none), in one retrieve, excluding anything from an
    installed managed package. Unlike retrieve_all_flows, the zip is handed
    back whole rather than unpacked - metadata-kb-worker.js takes the raw zip
    bytes, not pre-split files.
    """
    chosen = groups if groups is not None else _DEFAULT_ORG_SUMMARY_GROUPS
    unknown = sorted(set(chosen) - set(_ORG_SUMMARY_GROUP_TYPES))
    if unknown:
        raise RetrieveError(f"Unknown metadata group(s): {unknown}")
    types = [t for group in chosen for t in _ORG_SUMMARY_GROUP_TYPES[group]]

    async with MetadataClient(instance_url, session_id, api_version) as client:
        manifest = await _unmanaged_org_summary_types(client, types)
        if not manifest:
            raise RetrieveError(
                "No unmanaged metadata of the selected groups was found in "
                "this org - only components from installed packages, or "
                "nothing was selected."
            )
        job_id = await client.start_retrieve_types(manifest)
        return await client.wait_for_retrieve(job_id, timeout_seconds=600.0)


async def retrieve_flow(
    instance_url: str, session_id: str, flow_api_name: str, api_version: str = "62.0"
) -> str:
    """Pull one flow's metadata XML out of the org."""
    async with MetadataClient(instance_url, session_id, api_version) as client:
        job_id = await client.start_retrieve(flow_api_name)
        zip_bytes = await client.wait_for_retrieve(job_id)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        wanted = f"flows/{flow_api_name}.flow"
        names = archive.namelist()
        if wanted not in names:
            # Salesforce returns an empty package rather than an error when the
            # flow does not exist, so say that plainly.
            raise RetrieveError(
                f"{flow_api_name} was not in the retrieved package. "
                "Check the API name, or that the flow has a saved version."
            )
        return archive.read(wanted).decode("utf-8")


async def list_apex_classes(
    instance_url: str, session_id: str, api_version: str = "62.0"
) -> List[ApexClassSummary]:
    """
    Every Apex class in the org, newest first.

    Plain REST /query, not /tooling/query - ApexClass is a queryable standard
    object there too, and the regular endpoint is one less API surface to
    depend on for something this simple.
    """
    base = _normalise(instance_url)
    query = "SELECT Name, LastModifiedDate FROM ApexClass ORDER BY LastModifiedDate DESC"
    url = f"{base}/services/data/v{api_version}/query"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            url,
            params={"q": query},
            headers={"Authorization": f"Bearer {session_id}"},
        )
    if resp.status_code != 200:
        fault = _fault_string(resp.text)
        raise RetrieveError(
            fault or f"Could not list Apex classes ({resp.status_code}): {resp.text[:300]}"
        )

    return [
        ApexClassSummary(
            api_name=record["Name"],
            last_modified=record.get("LastModifiedDate"),
        )
        for record in resp.json().get("records", [])
    ]


async def retrieve_apex_class(
    instance_url: str, session_id: str, api_name: str, api_version: str = "62.0"
) -> str:
    """Pull one Apex class's source out of the org via plain REST query."""
    base = _normalise(instance_url)
    query = f"SELECT Body FROM ApexClass WHERE Name = '{api_name}'"
    url = f"{base}/services/data/v{api_version}/query"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            url,
            params={"q": query},
            headers={"Authorization": f"Bearer {session_id}"},
        )
    if resp.status_code != 200:
        fault = _fault_string(resp.text)
        raise RetrieveError(
            fault or f"Could not retrieve {api_name} ({resp.status_code}): {resp.text[:300]}"
        )
    records = resp.json().get("records", [])
    if not records:
        raise RetrieveError(f"{api_name} was not found in the org.")
    return records[0]["Body"]


async def validate_flow(
    instance_url: str, session_id: str, flow_api_name: str, flow_xml: str,
    api_version: str = "62.0", check_only: bool = True,
) -> DeployResult:
    """One-shot helper: package a Flow and run it through the org."""
    zip_bytes = build_package(flow_api_name, flow_xml, api_version)
    async with MetadataClient(instance_url, session_id, api_version) as client:
        return await client.deploy_and_wait(zip_bytes, check_only=check_only)


async def validate_bundle(
    instance_url: str, session_id: str, files: Dict[str, str], types: Dict[str, List[str]],
    api_version: str = "62.0", check_only: bool = True,
) -> DeployResult:
    """
    One-shot helper: package a mixed bundle - any combination of Object,
    Field, Apex, Flow, ... members - and run it through the org as a single
    deploy transaction. The multi-artifact counterpart to validate_flow,
    for when a plan's steps depend on each other (a Flow referencing a field
    that does not exist in the org until this same deploy creates it).
    """
    zip_bytes = build_deploy_package(files, types, api_version)
    async with MetadataClient(instance_url, session_id, api_version) as client:
        return await client.deploy_and_wait(zip_bytes, check_only=check_only)
