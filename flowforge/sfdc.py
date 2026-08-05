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
from typing import List, Optional

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


def build_package(flow_api_name: str, flow_xml: str, api_version: str) -> bytes:
    """Metadata-API-format ZIP: package.xml + flows/<name>.flow."""
    package_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Package xmlns="{METADATA_NS}">\n'
        "    <types>\n"
        f"        <members>{flow_api_name}</members>\n"
        "        <name>Flow</name>\n"
        "    </types>\n"
        f"    <version>{api_version}</version>\n"
        "</Package>\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package.xml", package_xml)
        archive.writestr(f"flows/{flow_api_name}.flow", flow_xml)
    return buffer.getvalue()


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


async def validate_flow(
    instance_url: str, session_id: str, flow_api_name: str, flow_xml: str,
    api_version: str = "62.0", check_only: bool = True,
) -> DeployResult:
    """One-shot helper: package a Flow and run it through the org."""
    zip_bytes = build_package(flow_api_name, flow_xml, api_version)
    async with MetadataClient(instance_url, session_id, api_version) as client:
        return await client.deploy_and_wait(zip_bytes, check_only=check_only)
