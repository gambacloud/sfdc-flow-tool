"""The Platform Event IR's job is to make invalid platform events
unrepresentable - same reasoning as test_ir_mdt.py, for the sibling __e IR
with its own (smaller) field-type set."""

import pytest
from pydantic import ValidationError

from flowtool.ir_platform_event import PlatformEvent, PlatformEventField

TEXT_FIELD = PlatformEventField(api_name="Notes", label="Notes", type="Text")


class TestApiNameSuffix:
    def test_e_suffix_is_appended(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[TEXT_FIELD],
        )
        assert event.api_name == "Order_Placed__e"

    def test_e_suffix_is_not_doubled(self):
        event = PlatformEvent(
            api_name="Order_Placed__e", label="Order Placed", plural_label="Order Placed",
            fields=[TEXT_FIELD],
        )
        assert event.api_name == "Order_Placed__e"

    def test_field_c_suffix_is_appended(self):
        field = PlatformEventField(api_name="Notes", label="Notes", type="Text")
        assert field.api_name == "Notes__c"

    @pytest.mark.parametrize("name", ["1_First", "trailing_", "double__under"])
    def test_rejects_invalid_base_name(self, name):
        with pytest.raises(ValidationError, match="not a valid Salesforce API name"):
            PlatformEvent(api_name=name, label="x", plural_label="x", fields=[TEXT_FIELD])


class TestPlatformEventShape:
    def test_requires_at_least_one_field(self):
        with pytest.raises(ValidationError):
            PlatformEvent(api_name="Empty", label="Empty", plural_label="Empty", fields=[])

    def test_default_publish_behavior(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[TEXT_FIELD],
        )
        assert event.publish_behavior == "PublishAfterCommit"

    def test_publish_immediately_is_accepted(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            publish_behavior="PublishImmediately", fields=[TEXT_FIELD],
        )
        assert event.publish_behavior == "PublishImmediately"


class TestPlatformEventFieldTypeShape:
    def test_text_defaults_length_255(self):
        field = PlatformEventField(api_name="Notes", label="Notes", type="Text")
        assert field.length == 255

    def test_length_on_non_text_is_rejected(self):
        with pytest.raises(ValidationError, match="length only applies to Text"):
            PlatformEventField(api_name="Notes", label="Notes", type="Checkbox", length=100)

    def test_number_requires_precision(self):
        with pytest.raises(ValidationError, match="requires precision"):
            PlatformEventField(api_name="Amount", label="Amount", type="Number")

    def test_precision_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="precision/scale only apply to"):
            PlatformEventField(api_name="Notes", label="Notes", type="Text", precision=5)

    @pytest.mark.parametrize("field_type", ["Checkbox", "Date", "DateTime", "LongTextArea"])
    def test_simple_types_need_no_extra_fields(self, field_type):
        field = PlatformEventField(api_name="X", label="X", type=field_type)
        assert field.type == field_type

    @pytest.mark.parametrize("bad_type", ["Picklist", "Lookup", "MasterDetail", "Currency", "Time"])
    def test_unsupported_field_types_are_rejected(self, bad_type):
        with pytest.raises(ValidationError):
            PlatformEventField(api_name="X", label="X", type=bad_type)

    # The three cases below were caught by a real checkOnly deploy against a
    # live dev org, not written speculatively: Salesforce rejected a
    # Checkbox with no defaultValue and a LongTextArea with no length/
    # visibleLines, on the first attempt.
    def test_checkbox_defaults_default_value_false(self):
        field = PlatformEventField(api_name="Is_Rush", label="Is Rush", type="Checkbox")
        assert field.default_value == "false"

    def test_default_value_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="default_value only applies to Checkbox"):
            PlatformEventField(api_name="Notes", label="Notes", type="Text", default_value="false")

    def test_long_text_area_defaults_length_and_visible_lines(self):
        field = PlatformEventField(api_name="Notes", label="Notes", type="LongTextArea")
        assert field.length == 32768
        assert field.visible_lines == 3

    def test_visible_lines_on_unrelated_type_is_rejected(self):
        with pytest.raises(ValidationError, match="visible_lines only applies to LongTextArea"):
            PlatformEventField(api_name="Notes", label="Notes", type="Text", visible_lines=3)
