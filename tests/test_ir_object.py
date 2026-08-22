"""The Object/Field IR's job is to make invalid custom metadata unrepresentable."""

import pytest
from pydantic import ValidationError

from flowtool.ir_object import CustomField, CustomObject


class TestApiNameSuffix:
    def test_object_suffix_is_appended(self):
        assert CustomObject(
            api_name="Invoice", label="Invoice", plural_label="Invoices"
        ).api_name == "Invoice__c"

    def test_object_suffix_is_not_doubled(self):
        assert CustomObject(
            api_name="Invoice__c", label="Invoice", plural_label="Invoices"
        ).api_name == "Invoice__c"

    def test_field_suffix_is_appended(self):
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
        )
        assert field.api_name == "Amount__c"

    def test_object_api_name_for_a_custom_object_is_left_alone(self):
        # __c is not auto-appended here the way it is for api_name - see
        # test_object_api_name_for_a_standard_object_is_not_suffixed for why:
        # this field is a real regression test, not a hypothetical one.
        field = CustomField(
            api_name="Amount", label="Amount", type="Number",
            object_api_name="Invoice__c", precision=18, scale=2,
        )
        assert field.object_api_name == "Invoice__c"

    def test_object_api_name_for_a_standard_object_is_not_suffixed(self):
        # The actual bug, caught live: object_api_name="Case" was silently
        # rewritten to "Case__c" - an object that does not exist - and the
        # deploy failed with a "not found in zipped directory" error that
        # gave no hint the object name itself had been corrupted.
        field = CustomField(
            api_name="SLA_Level", label="SLA Level", type="Text",
            object_api_name="Case",
        )
        assert field.object_api_name == "Case"

    def test_reference_to_a_standard_object_is_left_alone(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="Lookup",
            object_api_name="Invoice__c", reference_to="Account",
        )
        assert field.reference_to == "Account"

    @pytest.mark.parametrize("name", ["1_First", "trailing_", "double__under"])
    def test_rejects_invalid_base_name(self, name):
        with pytest.raises(ValidationError, match="not a valid Salesforce API name"):
            CustomObject(api_name=name, label="x", plural_label="x")

    def test_object_name_over_40_chars_is_rejected(self):
        # Salesforce's real limit on a custom object's base name (before __c).
        too_long = "A" * 41
        with pytest.raises(ValidationError, match="over Salesforce's 40-character limit"):
            CustomObject(api_name=too_long, label="x", plural_label="x")

    def test_object_name_at_exactly_40_chars_is_accepted(self):
        exactly = "A" * 40
        assert CustomObject(
            api_name=exactly, label="x", plural_label="x"
        ).api_name == f"{exactly}__c"

    def test_field_name_over_40_chars_is_rejected(self):
        too_long = "A" * 41
        with pytest.raises(ValidationError, match="over Salesforce's 40-character limit"):
            CustomField(
                api_name=too_long, label="x", type="Text", object_api_name="Invoice__c",
            )

    def test_custom_reference_to_over_40_chars_is_rejected(self):
        too_long = "A" * 41 + "__c"
        with pytest.raises(ValidationError, match="over Salesforce's 40-character limit"):
            CustomField(
                api_name="Link", label="Link", type="Lookup",
                object_api_name="Invoice__c", reference_to=too_long,
            )

    def test_standard_object_reference_to_is_not_length_checked(self):
        # "Account" is nowhere near 40 chars, but the point is that a standard
        # object reference takes the un-suffixed check path at all - this
        # would still pass a much longer standard name too.
        field = CustomField(
            api_name="Link", label="Link", type="Lookup",
            object_api_name="Invoice__c", reference_to="Account",
        )
        assert field.reference_to == "Account"


class TestCustomObject:
    def test_minimal_object_is_valid(self):
        obj = CustomObject(api_name="Invoice", label="Invoice", plural_label="Invoices")
        assert obj.record_name_type == "Text"
        assert obj.deployment_status == "Deployed"
        assert obj.sharing_model == "ReadWrite"

    def test_autonumber_requires_display_format(self):
        with pytest.raises(ValidationError, match="requires record_name_display_format"):
            CustomObject(
                api_name="Invoice", label="Invoice", plural_label="Invoices",
                record_name_type="AutoNumber",
            )

    def test_autonumber_with_format_is_valid(self):
        obj = CustomObject(
            api_name="Invoice", label="Invoice", plural_label="Invoices",
            record_name_type="AutoNumber", record_name_display_format="INV-{0000}",
        )
        assert obj.record_name_display_format == "INV-{0000}"

    def test_text_name_rejects_display_format(self):
        with pytest.raises(ValidationError, match="only applies to AutoNumber"):
            CustomObject(
                api_name="Invoice", label="Invoice", plural_label="Invoices",
                record_name_display_format="INV-{0000}",
            )


class TestCustomFieldTypeShapes:
    def test_text_defaults_length(self):
        field = CustomField(
            api_name="Notes", label="Notes", type="Text", object_api_name="Invoice__c",
        )
        assert field.length == 255

    def test_length_on_non_text_is_rejected(self):
        with pytest.raises(ValidationError, match="length only applies to Text"):
            CustomField(
                api_name="Amount", label="Amount", type="Number",
                object_api_name="Invoice__c", precision=18, length=10,
            )

    def test_number_requires_precision(self):
        with pytest.raises(ValidationError, match="Number requires precision"):
            CustomField(
                api_name="Amount", label="Amount", type="Number",
                object_api_name="Invoice__c",
            )

    def test_precision_on_non_number_is_rejected(self):
        with pytest.raises(ValidationError, match="only apply to Number"):
            CustomField(
                api_name="Notes", label="Notes", type="Text",
                object_api_name="Invoice__c", precision=18,
            )

    def test_picklist_requires_values(self):
        with pytest.raises(ValidationError, match="Picklist requires picklist_values"):
            CustomField(
                api_name="Status", label="Status", type="Picklist",
                object_api_name="Invoice__c",
            )

    def test_picklist_values_on_non_picklist_is_rejected(self):
        with pytest.raises(ValidationError, match="only applies to Picklist"):
            CustomField(
                api_name="Notes", label="Notes", type="Text",
                object_api_name="Invoice__c", picklist_values=["A"],
            )

    def test_lookup_requires_reference_to(self):
        with pytest.raises(ValidationError, match="Lookup requires reference_to"):
            CustomField(
                api_name="Account_Link", label="Account", type="Lookup",
                object_api_name="Invoice__c",
            )

    def test_reference_to_on_non_relationship_is_rejected(self):
        with pytest.raises(ValidationError, match="only applies to Lookup/MasterDetail"):
            CustomField(
                api_name="Notes", label="Notes", type="Text",
                object_api_name="Invoice__c", reference_to="Account",
            )

    def test_master_detail_rejects_explicit_required(self):
        with pytest.raises(ValidationError, match="always required"):
            CustomField(
                api_name="Account_Link", label="Account", type="MasterDetail",
                object_api_name="Invoice__c", reference_to="Account", required=True,
            )

    def test_master_detail_without_required_is_valid(self):
        field = CustomField(
            api_name="Account_Link", label="Account", type="MasterDetail",
            object_api_name="Invoice__c", reference_to="Account",
        )
        assert field.reference_to == "Account"
