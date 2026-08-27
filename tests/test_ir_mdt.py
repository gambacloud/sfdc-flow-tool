"""The MDT IR's job is to make invalid custom metadata unrepresentable -
same reasoning as test_ir_object.py, for the sibling __mdt/__c IR."""

import pytest
from pydantic import ValidationError

from flowtool.ir_mdt import CustomMetadataRecord, MetadataField, MetadataType

CHECKBOX = MetadataField(api_name="Enabled", label="Enabled", type="Checkbox")


class TestApiNameSuffix:
    def test_mdt_suffix_is_appended(self):
        mdt = MetadataType(
            api_name="Feature_Flag", label="Feature Flag", plural_label="Feature Flags",
            fields=[CHECKBOX],
        )
        assert mdt.api_name == "Feature_Flag__mdt"

    def test_mdt_suffix_is_not_doubled(self):
        mdt = MetadataType(
            api_name="Feature_Flag__mdt", label="Feature Flag", plural_label="Feature Flags",
            fields=[CHECKBOX],
        )
        assert mdt.api_name == "Feature_Flag__mdt"

    def test_field_c_suffix_is_appended(self):
        field = MetadataField(api_name="Enabled", label="Enabled", type="Checkbox")
        assert field.api_name == "Enabled__c"

    def test_record_type_api_name_gets_mdt_suffix(self):
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
        )
        assert record.type_api_name == "Feature_Flag__mdt"

    def test_record_developer_name_has_no_suffix_appended(self):
        record = CustomMetadataRecord(
            type_api_name="Feature_Flag", developer_name="New_UI", label="New UI",
        )
        assert record.developer_name == "New_UI"

    @pytest.mark.parametrize("name", ["1_First", "trailing_", "double__under"])
    def test_rejects_invalid_base_name(self, name):
        with pytest.raises(ValidationError, match="not a valid Salesforce API name"):
            MetadataType(api_name=name, label="x", plural_label="x", fields=[CHECKBOX])


class TestMetadataTypeShape:
    def test_requires_at_least_one_field(self):
        with pytest.raises(ValidationError):
            MetadataType(api_name="Empty", label="Empty", plural_label="Empties", fields=[])

    def test_defaults(self):
        mdt = MetadataType(
            api_name="Feature_Flag", label="Feature Flag", plural_label="Feature Flags",
            fields=[CHECKBOX],
        )
        assert mdt.visibility == "Public"
        assert mdt.deployment_status == "Deployed"


class TestMetadataFieldTypeShape:
    def test_text_defaults_length_255(self):
        field = MetadataField(api_name="Notes", label="Notes", type="Text")
        assert field.length == 255

    def test_length_on_non_text_is_rejected(self):
        with pytest.raises(ValidationError, match="length only applies to Text"):
            MetadataField(api_name="Notes", label="Notes", type="Checkbox", length=100)

    @pytest.mark.parametrize("field_type", ["Number", "Percent"])
    def test_number_and_percent_require_precision(self, field_type):
        with pytest.raises(ValidationError, match="requires precision"):
            MetadataField(api_name="Amount", label="Amount", type=field_type)

    def test_precision_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="precision/scale only apply to"):
            MetadataField(api_name="Notes", label="Notes", type="Text", precision=5)

    def test_picklist_requires_values(self):
        with pytest.raises(ValidationError, match="Picklist requires picklist_values"):
            MetadataField(api_name="Status", label="Status", type="Picklist")

    def test_picklist_values_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="picklist_values only applies to Picklist"):
            MetadataField(
                api_name="Notes", label="Notes", type="Text", picklist_values=["A"],
            )

    def test_metadata_relationship_requires_reference_to(self):
        with pytest.raises(ValidationError, match="MetadataRelationship requires reference_to"):
            MetadataField(api_name="Parent", label="Parent", type="MetadataRelationship")

    def test_metadata_relationship_target_gets_mdt_suffix(self):
        field = MetadataField(
            api_name="Parent", label="Parent", type="MetadataRelationship",
            reference_to="Feature_Flag",
        )
        assert field.reference_to == "Feature_Flag__mdt"

    def test_reference_to_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="reference_to only applies to MetadataRelationship"):
            MetadataField(
                api_name="Notes", label="Notes", type="Text", reference_to="Feature_Flag__mdt",
            )

    @pytest.mark.parametrize("field_type", [
        "TextArea", "LongTextArea", "Date", "DateTime", "Email", "Phone", "URL",
    ])
    def test_simple_types_need_no_extra_fields(self, field_type):
        field = MetadataField(api_name="X", label="X", type=field_type)
        assert field.type == field_type
