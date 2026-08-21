"""
Golden checks on the Apex compiler: the body passes through verbatim, and the
sidecar carries the two fields Salesforce actually reads from it.
"""

import xml.etree.ElementTree as ET

from flowtool.ir_apex import ApexClass
from flowtool.xmlgen_apex import METADATA_NS, generate_apex

NS = {"m": METADATA_NS}


class TestGenerateApex:
    def test_body_passes_through_verbatim(self):
        source = "public class Foo {\n    // a comment with { and } in it\n}"
        cls = ApexClass(api_name="Foo", body=source)
        body, _meta = generate_apex(cls)
        assert body == source

    def test_sidecar_carries_version_and_status(self):
        cls = ApexClass(
            api_name="Foo", body="public class Foo {}",
            api_version="60.0", status="Inactive",
        )
        _body, meta = generate_apex(cls)
        root = ET.fromstring(meta)
        assert root.tag == f"{{{METADATA_NS}}}ApexClass"
        assert root.find("m:apiVersion", NS).text == "60.0"
        assert root.find("m:status", NS).text == "Inactive"

    def test_sidecar_default_status_is_active(self):
        cls = ApexClass(api_name="Foo", body="public class Foo {}")
        _body, meta = generate_apex(cls)
        root = ET.fromstring(meta)
        assert root.find("m:status", NS).text == "Active"
