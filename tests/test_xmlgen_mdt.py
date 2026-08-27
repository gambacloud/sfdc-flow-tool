"""
Golden checks on the MDT compiler: a __mdt's .object content omits
sharingModel/nameField (neither applies) and carries visibility (which a
regular CustomObject never does); a record's xsi:type is resolved from the
real field type when the MetadataType is known, and from a cheap heuristic
on the value string otherwise.
"""

import xml.etree.ElementTree as ET

from flowtool.ir_mdt import CustomMetadataRecord, MetadataField, MetadataType
from flowtool.xmlgen_mdt import METADATA_NS, XSI_NS, generate_mdt_record, generate_mdt_type

NS = {"m": METADATA_NS}
CHECKBOX = MetadataField(api_name="Enabled", label="Enabled", type="Checkbox")


class TestGenerateMdtType:
    def test_no_sharing_model_or_name_field(self):
        mdt = MetadataType(
            api_name="Feature_Flag", label="Feature Flag", plural_label="Feature Flags",
            fields=[CHECKBOX],
        )
        xml = generate_mdt_type(mdt)
        root = ET.fromstring(xml)
        assert root.find("m:sharingModel", NS) is None
        assert root.find("m:nameField", NS) is None

    def test_visibility_is_present(self):
        mdt = MetadataType(
            api_name="Feature_Flag", label="Feature Flag", plural_label="Feature Flags",
            visibility="Protected", fields=[CHECKBOX],
        )
        xml = generate_mdt_type(mdt)
        root = ET.fromstring(xml)
        assert root.find("m:visibility", NS).text == "Protected"

    def test_fields_are_embedded_sorted_by_api_name(self):
        f1 = MetadataField(api_name="Zeta", label="Zeta", type="Text")
        f2 = MetadataField(api_name="Alpha", label="Alpha", type="Text")
        mdt = MetadataType(
            api_name="Ordering", label="Ordering", plural_label="Orderings", fields=[f1, f2],
        )
        xml = generate_mdt_type(mdt)
        root = ET.fromstring(xml)
        names = [f.find("m:fullName", NS).text for f in root.findall("m:fields", NS)]
        assert names == ["Alpha__c", "Zeta__c"]

    def test_metadata_relationship_field_carries_reference_to(self):
        rel = MetadataField(
            api_name="Parent", label="Parent", type="MetadataRelationship",
            reference_to="Feature_Flag",
        )
        mdt = MetadataType(
            api_name="Child_Type", label="Child", plural_label="Children", fields=[rel],
        )
        xml = generate_mdt_type(mdt)
        root = ET.fromstring(xml)
        field_el = root.find("m:fields", NS)
        assert field_el.find("m:referenceTo", NS).text == "Feature_Flag__mdt"
        assert field_el.find("m:type", NS).text == "MetadataRelationship"


class TestGenerateMdtRecord:
    def test_xsi_namespace_declared(self):
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
            values={"Enabled__c": "true"},
        )
        xml = generate_mdt_record(record)
        assert f'xmlns:xsi="{XSI_NS}"' in xml

    def test_xsi_type_resolved_from_real_field_type_when_known(self):
        num = MetadataField(api_name="Threshold", label="Threshold", type="Number", precision=5, scale=2)
        mdt = MetadataType(
            api_name="Feature_Flag", label="Feature Flag", plural_label="Feature Flags",
            fields=[CHECKBOX, num],
        )
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
            values={"Enabled__c": "true", "Threshold__c": "3.5"},
        )
        xml = generate_mdt_record(record, mdt)
        root = ET.fromstring(xml)
        types_by_field = {
            v.find("m:field", NS).text: v.find("m:value", NS).get(f"{{{XSI_NS}}}type")
            for v in root.findall("m:values", NS)
        }
        assert types_by_field == {"Enabled__c": "xsd:boolean", "Threshold__c": "xsd:double"}

    def test_xsi_type_falls_back_to_heuristic_without_a_known_type(self):
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
            values={"Enabled__c": "true", "Threshold__c": "3.5", "Notes__c": "hello"},
        )
        xml = generate_mdt_record(record, mdt_type=None)
        root = ET.fromstring(xml)
        types_by_field = {
            v.find("m:field", NS).text: v.find("m:value", NS).get(f"{{{XSI_NS}}}type")
            for v in root.findall("m:values", NS)
        }
        assert types_by_field == {
            "Enabled__c": "xsd:boolean", "Threshold__c": "xsd:double", "Notes__c": "xsd:string",
        }

    def test_protected_and_label(self):
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
            protected=True,
        )
        xml = generate_mdt_record(record)
        root = ET.fromstring(xml)
        assert root.find("m:label", NS).text == "New UI"
        assert root.find("m:protected", NS).text == "true"
