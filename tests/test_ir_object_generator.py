"""
CustomObjectGenerator/CustomFieldGenerator reuse the generic repair loop from
llm.py's IRGenerator - these pin that the loop actually drives a non-Flow
model end to end (schema, validate, repair), not just Flow.
"""

from flowtool.ir_object import CustomField, CustomObject
from flowtool.llm import CustomFieldGenerator, CustomObjectGenerator
from tests.test_llm import ScriptedProvider

VALID_OBJECT = {
    "api_name": "Invoice",
    "label": "Invoice",
    "plural_label": "Invoices",
}

INVALID_OBJECT = {
    # AutoNumber without a display format - a real validation failure.
    "api_name": "Invoice",
    "label": "Invoice",
    "plural_label": "Invoices",
    "record_name_type": "AutoNumber",
}

VALID_FIELD = {
    "api_name": "Amount",
    "label": "Amount",
    "type": "Number",
    "object_api_name": "Invoice__c",
    "precision": 18,
    "scale": 2,
}


class TestCustomObjectGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID_OBJECT)
        result = CustomObjectGenerator(provider).generate("an Invoice object")
        assert result.repairs == 0
        assert isinstance(result.value, CustomObject)
        assert result.value.api_name == "Invoice__c"

    def test_invalid_shape_is_repaired(self):
        provider = ScriptedProvider(INVALID_OBJECT, VALID_OBJECT)
        result = CustomObjectGenerator(provider).generate("an Invoice object")
        assert result.repairs == 1
        assert result.value.api_name == "Invoice__c"

        complaint = provider.calls[1][-1].content
        assert "failed validation" in complaint


class TestCustomFieldGenerator:
    def test_valid_first_try(self):
        provider = ScriptedProvider(VALID_FIELD)
        result = CustomFieldGenerator(provider).generate("an Amount field on Invoice")
        assert result.repairs == 0
        assert isinstance(result.value, CustomField)
        assert result.value.api_name == "Amount__c"
        assert result.value.object_api_name == "Invoice__c"
