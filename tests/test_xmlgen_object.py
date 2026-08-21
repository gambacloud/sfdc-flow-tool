"""
Golden checks on the CustomObject/CustomField compiler. Each assertion
corresponds to something Salesforce rejects or silently mishandles, so a
regression is caught without touching an org.
"""

import xml.etree.ElementTree as ET

from flowtool.ir_object import CustomField, CustomObject
from flowtool.xmlgen_object import METADATA_NS, generate_field, generate_object

NS = {"m": METADATA_NS}


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _text(root: ET.Element, tag: str):
    node = root.find(f"m:{tag}", NS)
    return node.text if node is not None else None


class TestObjectXml:
    def test_text_name_field(self):
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        root = _root(generate_object(obj))
        assert root.tag == f"{{{METADATA_NS}}}CustomObject"
        assert _text(root, "label") == "Invoice"
        assert _text(root, "pluralLabel") == "Invoices"
        assert _text(root, "deploymentStatus") == "Deployed"
        assert _text(root, "sharingModel") == "ReadWrite"
        name_field = root.find("m:nameField", NS)
        assert _text(name_field, "label") == "Name"
        assert _text(name_field, "type") == "Text"
        assert name_field.find("m:displayFormat", NS) is None

    def test_autonumber_name_field(self):
        obj = CustomObject(
            api_name="Invoice", label="Invoice", plural_label="Invoices",
            record_name_type="AutoNumber", record_name_display_format="INV-{0000}",
        )
        name_field = _root(generate_object(obj)).find("m:nameField", NS)
        assert _text(name_field, "displayFormat") == "INV-{0000}"
        assert _text(name_field, "type") == "AutoNumber"

    def test_no_description_when_none_given(self):
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        root = _root(generate_object(obj))
        assert root.find("m:description", NS) is None


class TestFieldXml:
    def test_text_field(self):
        field = CustomField(
            api_name="Notes", label="Notes", type="Text",
            object_api_name="Invoice__c", required=True, unique=True,
        )
        root = _root(generate_field(field))
        assert root.tag == f"{{{METADATA_NS}}}CustomField"
        assert _text(root, "fullName") == "Notes__c"
        assert _text(root, "length") == "255"
        assert _text(root, "required") == "true"
        assert _text(root, "unique") == "true"
        assert _text(root, "type") == "Text"

    def test_number_field(self):
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
        )
        root = _root(generate_field(field))
        assert _text(root, "precision") == "18"
        assert _text(root, "scale") == "2"
        assert _text(root, "type") == "Number"

    def test_checkbox_always_has_a_default(self):
        field = CustomField(
            api_name="Is_Paid", label="Is Paid", type="Checkbox",
            object_api_name="Invoice__c",
        )
        root = _root(generate_field(field))
        assert _text(root, "defaultValue") == "false"
        assert root.find("m:required", NS) is None

    def test_checkbox_honours_an_explicit_default(self):
        field = CustomField(
            api_name="Is_Paid", label="Is Paid", type="Checkbox",
            object_api_name="Invoice__c", default_value="true",
        )
        root = _root(generate_field(field))
        assert _text(root, "defaultValue") == "true"

    def test_picklist_values(self):
        field = CustomField(
            api_name="Status", label="Status", type="Picklist",
            object_api_name="Invoice__c", picklist_values=["Draft", "Sent", "Paid"],
        )
        root = _root(generate_field(field))
        entries = root.findall("m:valueSet/m:valueSetDefinition/m:value", NS)
        assert [_text(e, "fullName") for e in entries] == ["Draft", "Sent", "Paid"]

    def test_lookup_field(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="Lookup",
            object_api_name="Invoice__c", reference_to="Account",
            relationship_name="Invoices",
        )
        root = _root(generate_field(field))
        assert _text(root, "referenceTo") == "Account"
        assert _text(root, "relationshipName") == "Invoices"
        assert _text(root, "required") == "false"

    def test_master_detail_field_has_no_required_tag(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="MasterDetail",
            object_api_name="Invoice__c", reference_to="Account",
        )
        root = _root(generate_field(field))
        assert root.find("m:required", NS) is None

    def test_children_are_alphabetical(self):
        # The Metadata API is picky about child order for some types, the same
        # way it is for Flow XML - a regression here is easy to miss by eye.
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
            description="The invoice total", required=True, unique=True,
        )
        root = _root(generate_field(field))
        tags = [child.tag.split("}")[-1] for child in root]
        assert tags == sorted(tags)
