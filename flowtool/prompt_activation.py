"""
Making a deployed Prompt Template runnable.

A template deployed as Published is still not callable: running it answers
"No active template version" until the template names its active version. The
version's identifier is assigned by the org on the first deploy (it is not
derivable from the content - a made-up one is rejected), so activation is a
second step: retrieve what the org created, read the identifier, and deploy the
same file again with `activeVersionIdentifier` set. Verified against a real
scratch org: before it the generations API fails, after it returns the model's
JSON.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional

from .sfdc import MetadataClient, build_deploy_package

_VERSION_RE = re.compile(r"<versionIdentifier>([^<]+)</versionIdentifier>")
_ACTIVE_RE = re.compile(r"<activeVersionIdentifier>([^<]+)</activeVersionIdentifier>")
_FIRST_DESCRIPTION_RE = re.compile(r"<description>")
_FIRST_DEVNAME_RE = re.compile(r"<developerName>")


def with_active_version(xml: str) -> Optional[str]:
    """The retrieved template file with its newest version made the active one,
    or None when it is already active or carries no version yet."""
    versions = _VERSION_RE.findall(xml)
    if not versions or _ACTIVE_RE.search(xml):
        return None
    marker = _FIRST_DEVNAME_RE  # a direct child of the template, always present
    return marker.sub(f"<activeVersionIdentifier>{versions[-1]}</activeVersionIdentifier>\n    <developerName>", xml, count=1)


def _files_from_zip(zip_bytes: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.startswith("genAiPromptTemplates/") and name.endswith(".genAiPromptTemplate"):
                out[name[len("genAiPromptTemplates/"):-len(".genAiPromptTemplate")]] = archive.read(name).decode("utf-8")
    return out


async def activate_templates(
    client: MetadataClient, names: List[str], api_version: str,
) -> List[str]:
    """Activate each named, already-deployed template. Returns the names that
    were activated now (those already active are left alone). Raises RuntimeError
    with the org's own message when the second deploy is rejected."""
    if not names:
        return []
    zip_bytes = await client.wait_for_retrieve(await client.start_retrieve_types({"GenAiPromptTemplate": names}))
    retrieved = _files_from_zip(zip_bytes)
    missing = [n for n in names if n not in retrieved]
    if missing:
        raise RuntimeError(f"could not read back {', '.join(missing)} to activate it")

    files: Dict[str, str] = {}
    for name in names:
        updated = with_active_version(retrieved[name])
        if updated is not None:
            files[f"genAiPromptTemplates/{name}.genAiPromptTemplate"] = updated
    if not files:
        return []

    members = [path.split("/")[1].removesuffix(".genAiPromptTemplate") for path in files]
    result = await client.deploy_and_wait(
        build_deploy_package(files, {"GenAiPromptTemplate": members}, api_version), check_only=False,
    )
    if not result.success:
        detail = "; ".join(str(f) for f in result.failures) or result.error_message or result.status
        raise RuntimeError(detail)
    return members
