"""
ApexClass IR -> deployable metadata.

Apex has no XML body to render - the `.cls` file *is* the class source,
verbatim. Only the `.cls-meta.xml` sidecar is generated here, the same pairing
Salesforce itself writes on retrieve.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Tuple

from .ir_apex import ApexClass, ApexTrigger
from .xmlgen import METADATA_NS, _sub


def generate_apex(cls: ApexClass) -> Tuple[str, str]:
    """
    Render a validated ApexClass IR as deployable metadata.

    Returns `(body, meta_xml)`: the `.cls` file content and its `.cls-meta.xml`
    sidecar, matching how the Metadata API expects an Apex class - two
    separate members sharing one name, not one XML document.
    """
    return cls.body, _meta_xml("ApexClass", cls.api_version, cls.status)


def generate_apex_trigger(trigger: ApexTrigger) -> Tuple[str, str]:
    """
    Render a validated ApexTrigger IR as deployable metadata.

    Returns `(body, meta_xml)`: the `.trigger` file content and its
    `.trigger-meta.xml` sidecar - same shape as generate_apex(), just the
    other root element name, since a trigger's own metadata is exactly as
    thin as a class's (apiVersion and status, nothing else).
    """
    return trigger.body, _meta_xml("ApexTrigger", trigger.api_version, trigger.status)


def _meta_xml(root_element: str, api_version: str, status: str) -> str:
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}{root_element}")
    _sub(root, "apiVersion", api_version)
    _sub(root, "status", status)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
