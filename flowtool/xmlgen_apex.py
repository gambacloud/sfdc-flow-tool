"""
ApexClass IR -> deployable metadata.

Apex has no XML body to render - the `.cls` file *is* the class source,
verbatim. Only the `.cls-meta.xml` sidecar is generated here, the same pairing
Salesforce itself writes on retrieve.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Tuple

from .ir_apex import ApexClass
from .xmlgen import METADATA_NS, _sub


def generate_apex(cls: ApexClass) -> Tuple[str, str]:
    """
    Render a validated ApexClass IR as deployable metadata.

    Returns `(body, meta_xml)`: the `.cls` file content and its `.cls-meta.xml`
    sidecar, matching how the Metadata API expects an Apex class - two
    separate members sharing one name, not one XML document.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}ApexClass")
    _sub(root, "apiVersion", cls.api_version)
    _sub(root, "status", cls.status)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    meta_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
    return cls.body, meta_xml
