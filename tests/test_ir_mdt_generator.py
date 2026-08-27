"""
MetadataTypeGenerator/CustomMetadataRecordGenerator reuse llm.py's generic
repair loop - unlike Apex/LWC, this IR is fully structured (no free-form
body), so plain Pydantic validation is the whole gate, same as
CustomObjectGenerator/CustomFieldGenerator.
"""

from flowtool.ir_mdt import CustomMetadataRecord, MetadataType
from flowtool.llm import CustomMetadataRecordGenerator, MetadataTypeGenerator
from tests.test_llm import ScriptedProvider

VALID_MDT = {
    "api_name": "Feature_Flag",
    "label": "Feature Flag",
    "plural_label": "Feature Flags",
    "fields": [
        {"api_name": "Enabled", "label": "Enabled", "type": "Checkbox"},
    ],
}

INVALID_MDT_NO_FIELDS = {
    "api_name": "Feature_Flag",
    "label": "Feature Flag",
    "plural_label": "Feature Flags",
    "fields": [],
}

VALID_RECORD = {
    "type_api_name": "Feature_Flag",
    "developer_name": "New_UI",
    "label": "New UI",
    "values": {"Enabled__c": "true"},
}

INVALID_RECORD_BAD_NAME = {
    "type_api_name": "Feature_Flag",
    "developer_name": "1_Bad",
    "label": "New UI",
    "values": {},
}


class TestMetadataTypeGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID_MDT)
        result = MetadataTypeGenerator(provider).generate("a feature flag type")
        assert result.repairs == 0
        assert isinstance(result.value, MetadataType)
        assert result.value.api_name == "Feature_Flag__mdt"
        assert result.value.fields[0].api_name == "Enabled__c"

    def test_empty_fields_is_repaired(self):
        provider = ScriptedProvider(INVALID_MDT_NO_FIELDS, VALID_MDT)
        result = MetadataTypeGenerator(provider).generate("...")
        assert result.repairs == 1


class TestCustomMetadataRecordGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID_RECORD)
        result = CustomMetadataRecordGenerator(provider).generate("a New UI flag record")
        assert result.repairs == 0
        assert isinstance(result.value, CustomMetadataRecord)
        assert result.value.type_api_name == "Feature_Flag__mdt"
        assert result.value.values == {"Enabled__c": "true"}

    def test_invalid_developer_name_is_repaired(self):
        provider = ScriptedProvider(INVALID_RECORD_BAD_NAME, VALID_RECORD)
        result = CustomMetadataRecordGenerator(provider).generate("...")
        assert result.repairs == 1
