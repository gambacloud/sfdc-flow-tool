"""
PlatformEventGenerator reuses llm.py's generic repair loop - unlike Apex/LWC,
this IR is fully structured (no free-form body), so plain Pydantic validation
is the whole gate, same as CustomObjectGenerator/MetadataTypeGenerator.
"""

from flowtool.ir_platform_event import PlatformEvent
from flowtool.llm import PlatformEventGenerator
from tests.test_llm import ScriptedProvider

VALID_EVENT = {
    "api_name": "Order_Placed",
    "label": "Order Placed",
    "plural_label": "Order Placed",
    "fields": [
        {"api_name": "AccountId", "label": "Account Id", "type": "Text"},
        {"api_name": "Amount", "label": "Amount", "type": "Number", "precision": 16, "scale": 2},
    ],
}

INVALID_EVENT_NO_FIELDS = {
    "api_name": "Order_Placed",
    "label": "Order Placed",
    "plural_label": "Order Placed",
    "fields": [],
}


class TestPlatformEventGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID_EVENT)
        result = PlatformEventGenerator(provider).generate("an order placed event")
        assert result.repairs == 0
        assert isinstance(result.value, PlatformEvent)
        assert result.value.api_name == "Order_Placed__e"
        assert result.value.fields[0].api_name == "AccountId__c"

    def test_empty_fields_is_repaired(self):
        provider = ScriptedProvider(INVALID_EVENT_NO_FIELDS, VALID_EVENT)
        result = PlatformEventGenerator(provider).generate("...")
        assert result.repairs == 1
