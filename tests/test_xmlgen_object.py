"""
Golden checks on the CustomObject/CustomField compiler. Each assertion
corresponds to something Salesforce rejects or silently mishandles, so a
regression is caught without touching an org.
"""

import xml.etree.ElementTree as ET

from flowtool.ir_object import CustomField, CustomObject
from flowtool.xmlgen_object import METADATA_NS, generate_field_delta, generate_object

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

    def test_no_fields_element_when_none_given(self):
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        root = _root(generate_object(obj))
        assert root.find("m:fields", NS) is None

    def test_embedded_fields_carry_their_own_shape(self):
        # The point of embedding (see xmlgen_object.py's module docstring):
        # a new object's fields deploy inside the .object file, with the
        # exact same per-field content generate_field would produce
        # standalone - just nested under <fields> instead of being their
        # own document.
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        amount = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
        )
        status = CustomField(
            api_name="Status", label="Status", type="Picklist",
            object_api_name="Invoice__c", picklist_values=["Draft", "Paid"],
        )
        root = _root(generate_object(obj, [amount, status]))
        fields = root.findall("m:fields", NS)
        assert [_text(f, "fullName") for f in fields] == ["Amount__c", "Status__c"]
        assert _text(fields[0], "precision") == "18"
        assert [_text(v, "fullName") for v in fields[1].findall(
            "m:valueSet/m:valueSetDefinition/m:value", NS
        )] == ["Draft", "Paid"]

    def test_fields_element_sits_between_description_and_label(self):
        # CustomObject's own alphabetical child order - deploymentStatus,
        # description, fields, label, ... - matters to the Metadata API the
        # same way it does for every other type this build generates.
        obj = CustomObject(
            api_name="Invoice", label="Invoice", plural_label="Invoices",
            description="Invoices",
        )
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18,
        )
        root = _root(generate_object(obj, [field]))
        tags = [child.tag.split("}")[-1] for child in root]
        assert tags.index("description") < tags.index("fields") < tags.index("label")

    def test_embedded_fields_are_sorted_by_api_name(self):
        # Stable output regardless of the order steps happened to run in -
        # useful for diffing, and not incidental to correctness either.
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        z_field = CustomField(
            api_name="Zeta", label="Zeta", type="Text", object_api_name="Invoice__c",
        )
        a_field = CustomField(
            api_name="Alpha", label="Alpha", type="Text", object_api_name="Invoice__c",
        )
        root = _root(generate_object(obj, [z_field, a_field]))
        names = [_text(f, "fullName") for f in root.findall("m:fields", NS)]
        assert names == ["Alpha__c", "Zeta__c"]


class TestFieldDeltaXml:
    """
    generate_field_delta produces a *partial* CustomObject document - for
    fields added to an object this deploy does not also create. Confirmed
    live and against Salesforce's own `sf project convert source` output:
    see xmlgen_object.py's module docstring for why there is no separate
    per-field document format.
    """

    def _one_field(self, field: CustomField) -> ET.Element:
        root = _root(generate_field_delta([field]))
        assert root.tag == f"{{{METADATA_NS}}}CustomObject"
        fields = root.findall("m:fields", NS)
        assert len(fields) == 1
        return fields[0]

    def test_document_root_is_customobject_not_customfield(self):
        # The whole point: there is no <CustomField> document root here.
        field = CustomField(
            api_name="Notes", label="Notes", type="Text", object_api_name="Invoice__c",
        )
        root = _root(generate_field_delta([field]))
        assert root.tag == f"{{{METADATA_NS}}}CustomObject"
        assert root.find("m:deploymentStatus", NS) is None
        assert root.find("m:label", NS) is None
        assert root.find("m:sharingModel", NS) is None

    def test_text_field(self):
        field = CustomField(
            api_name="Notes", label="Notes", type="Text",
            object_api_name="Invoice__c", required=True, unique=True,
        )
        el = self._one_field(field)
        assert _text(el, "fullName") == "Notes__c"
        assert _text(el, "length") == "255"
        assert _text(el, "required") == "true"
        assert _text(el, "unique") == "true"
        assert _text(el, "type") == "Text"

    def test_number_field(self):
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
        )
        el = self._one_field(field)
        assert _text(el, "precision") == "18"
        assert _text(el, "scale") == "2"
        assert _text(el, "type") == "Number"

    def test_checkbox_always_has_a_default(self):
        field = CustomField(
            api_name="Is_Paid", label="Is Paid", type="Checkbox",
            object_api_name="Invoice__c",
        )
        el = self._one_field(field)
        assert _text(el, "defaultValue") == "false"
        assert el.find("m:required", NS) is None

    def test_checkbox_honours_an_explicit_default(self):
        field = CustomField(
            api_name="Is_Paid", label="Is Paid", type="Checkbox",
            object_api_name="Invoice__c", default_value="true",
        )
        el = self._one_field(field)
        assert _text(el, "defaultValue") == "true"

    def test_picklist_values(self):
        field = CustomField(
            api_name="Status", label="Status", type="Picklist",
            object_api_name="Invoice__c", picklist_values=["Draft", "Sent", "Paid"],
        )
        el = self._one_field(field)
        entries = el.findall("m:valueSet/m:valueSetDefinition/m:value", NS)
        assert [_text(e, "fullName") for e in entries] == ["Draft", "Sent", "Paid"]

    def test_picklist_is_restricted_by_default(self):
        field = CustomField(
            api_name="Status", label="Status", type="Picklist",
            object_api_name="Invoice__c", picklist_values=["Draft", "Paid"],
        )
        el = self._one_field(field)
        assert _text(el.find("m:valueSet", NS), "restricted") == "true"

    def test_picklist_restricted_can_be_turned_off(self):
        field = CustomField(
            api_name="Status", label="Status", type="Picklist",
            object_api_name="Invoice__c", picklist_values=["Draft", "Paid"],
            restricted=False,
        )
        el = self._one_field(field)
        assert _text(el.find("m:valueSet", NS), "restricted") == "false"

    def test_lookup_field(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="Lookup",
            object_api_name="Invoice__c", reference_to="Account",
            relationship_name="Invoices",
        )
        el = self._one_field(field)
        assert _text(el, "referenceTo") == "Account"
        assert _text(el, "relationshipName") == "Invoices"
        assert _text(el, "required") == "false"

    def test_master_detail_field_has_no_required_tag(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="MasterDetail",
            object_api_name="Invoice__c", reference_to="Account",
        )
        el = self._one_field(field)
        assert el.find("m:required", NS) is None

    def test_children_are_alphabetical(self):
        # The Metadata API is picky about child order for some types, the same
        # way it is for Flow XML - a regression here is easy to miss by eye.
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
            description="The invoice total", required=True, unique=True,
        )
        el = self._one_field(field)
        tags = [child.tag.split("}")[-1] for child in el]
        assert tags == sorted(tags)

    def test_multiple_fields_for_the_same_object_share_one_delta_file(self):
        # server.py's _bundle_files_and_types groups fields by object before
        # calling this - this pins down what it's relying on: several fields
        # land in one document, sorted, not one overwriting the last.
        amount = CustomField(
            api_name="Zeta", label="Zeta", type="Text", object_api_name="Case",
        )
        status = CustomField(
            api_name="Alpha", label="Alpha", type="Text", object_api_name="Case",
        )
        root = _root(generate_field_delta([amount, status]))
        names = [_text(f, "fullName") for f in root.findall("m:fields", NS)]
        assert names == ["Alpha__c", "Zeta__c"]
