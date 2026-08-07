"""
The repair loop, exercised with a scripted provider - no API key, no network.

What matters is that an invalid IR from the model turns into a corrected one
without a human in the loop, and that the model is told exactly what was wrong.
"""

import json

import pytest

from flowtool.llm import (
    FlowGenerator,
    LLMError,
    Message,
    Usage,
    gemini_schema,
    strict_schema,
)
from flowtool.ir import Flow

VALID = {
    "api_name": "Won_Deal_Flow",
    "label": "Won Deal Flow",
    "start": {
        "object": "Opportunity",
        "record_trigger_type": "CreateAndUpdate",
        "trigger_type": "RecordAfterSave",
        "next": "Get_Account",
    },
    "elements": [
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ],
}


def _with(**changes):
    payload = json.loads(json.dumps(VALID))
    payload.update(changes)
    return payload


DANGLING = _with(
    elements=[
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": "Does_Not_Exist",
        }
    ]
)

BAD_NAME = _with(
    start={**VALID["start"], "next": "Get Account"},
    elements=[
        {
            "type": "GetRecords",
            "name": "Get Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ],
)


class ScriptedProvider:
    """Returns each queued payload in turn and records what it was asked."""

    name = "scripted"

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.usage = Usage()

    def complete_json(self, system, messages, schema):
        self.calls.append(list(messages))
        self.usage.add(input_tokens=100, output_tokens=50)
        return self.payloads.pop(0)


class TestRepairLoop:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID)
        result = FlowGenerator(provider).generate("when a deal is won, get the account")
        assert result.repairs == 0
        assert isinstance(result.flow, Flow)
        assert result.flow.api_name == "Won_Deal_Flow"

    def test_dangling_reference_is_repaired(self):
        provider = ScriptedProvider(DANGLING, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1
        assert result.flow.elements[0].next is None

    def test_invalid_api_name_is_repaired(self):
        provider = ScriptedProvider(BAD_NAME, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1

    def test_model_is_told_what_was_wrong(self):
        provider = ScriptedProvider(DANGLING, VALID)
        FlowGenerator(provider).generate("...")

        # Second call must carry the rejected IR and the validator's complaint.
        second = provider.calls[1]
        assert second[-2].role == "assistant"
        complaint = second[-1].content
        assert "failed validation" in complaint
        assert "unresolved references" in complaint
        assert "Does_Not_Exist" in complaint

    def test_gives_up_after_the_repair_budget(self):
        provider = ScriptedProvider(DANGLING, DANGLING, DANGLING)
        with pytest.raises(LLMError, match="Could not get valid IR"):
            FlowGenerator(provider, max_repairs=2).generate("...")
        assert not provider.payloads, "should have used its whole budget"

    def test_deleting_the_orphan_instead_of_fixing_it_is_rejected(self):
        # A model repairing "unreachable elements" can trivially satisfy the
        # check by deleting the orphan instead of adding the missing
        # connector. That must not be accepted as a fix.
        shrunk = _with(elements=[])
        provider = ScriptedProvider(DANGLING, shrunk, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 2
        assert [e.name for e in result.flow.elements] == ["Get_Account"]

        complaint = provider.calls[2][-1].content
        assert "Get_Account" in complaint
        assert "not a fix" in complaint

    def test_gives_up_when_the_model_only_ever_shrinks(self):
        shrunk = _with(elements=[])
        provider = ScriptedProvider(DANGLING, shrunk, shrunk)
        with pytest.raises(LLMError, match="Could not get valid IR"):
            FlowGenerator(provider, max_repairs=2).generate("...")
        assert not provider.payloads, "should have used its whole budget"


class TestRefine:
    def test_refinement_continues_the_conversation(self):
        provider = ScriptedProvider(VALID, VALID)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        generator.refine(first, "also mark the account hot")

        second = provider.calls[1]
        assert second[0].content == "build it", "lost the original request"
        assert second[-1].content == "also mark the account hot"

    def test_salesforce_errors_are_fed_back(self):
        provider = ScriptedProvider(VALID, VALID)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        generator.repair_from_salesforce(
            first, ["[Error] Mark_Hot (Update Records) - You can't use the sObjectInputReference field"]
        )

        instruction = provider.calls[1][-1].content
        assert "Salesforce rejected" in instruction
        assert "sObjectInputReference" in instruction


RAW = Flow.model_json_schema()

# The exact keyword set Gemini's response_json_schema documents.
GEMINI_SUPPORTED = {
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description",
    "enum", "items", "prefixItems", "minItems", "maxItems", "minimum", "maximum",
    "anyOf", "oneOf", "properties", "additionalProperties", "required",
    "propertyOrdering",
}

NAME_KEYED = {"properties", "$defs"}


def keywords(schema, path="root"):
    """Yield (path, keyword) for real schema keywords, skipping field names."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in NAME_KEYED and isinstance(value, dict):
                for name, sub in value.items():
                    yield from keywords(sub, f"{path}.{key}.{name}")
                continue
            yield path, key
            yield from keywords(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from keywords(value, f"{path}[{index}]")


def open_objects(schema, path="root"):
    if isinstance(schema, dict):
        if (schema.get("type") == "object" or "properties" in schema) and \
                schema.get("additionalProperties") is not False:
            yield path
        for key, value in schema.items():
            if key in NAME_KEYED and isinstance(value, dict):
                for name, sub in value.items():
                    yield from open_objects(sub, f"{path}.{key}.{name}")
            else:
                yield from open_objects(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from open_objects(value, f"{path}[{index}]")


class TestFieldNamesSurvive:
    """
    Regression: an allowlist filter that recurses into `properties` treats field
    names as keywords and deletes the entire document.
    """

    @pytest.mark.parametrize("adapt", [strict_schema, gemini_schema])
    def test_top_level_fields_are_kept(self, adapt):
        assert set(adapt(RAW)["properties"]) == set(RAW["properties"])

    @pytest.mark.parametrize("adapt", [strict_schema, gemini_schema])
    def test_every_definition_keeps_its_fields(self, adapt):
        adapted = adapt(RAW)
        assert set(adapted["$defs"]) == set(RAW["$defs"])
        for name, definition in RAW["$defs"].items():
            if "properties" in definition:
                assert set(adapted["$defs"][name]["properties"]) == set(
                    definition["properties"]
                ), f"{name} lost fields"


class TestAnthropicDialect:
    def test_objects_are_closed(self):
        assert not list(open_objects(strict_schema(RAW)))

    def test_unsupported_constraints_are_gone(self):
        unsupported = {"minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}
        found = {k for _, k in keywords(strict_schema(RAW)) if k in unsupported}
        assert not found, found

    def test_const_discriminator_is_preserved(self):
        # Anthropic supports const, so it stays as the tighter constraint.
        assert strict_schema(RAW)["$defs"]["GetRecords"]["properties"]["type"]["const"] == "GetRecords"


class TestGeminiDialect:
    def test_only_supported_keywords_survive(self):
        found = {k for _, k in keywords(gemini_schema(RAW)) if k not in GEMINI_SUPPORTED}
        assert not found, f"Gemini would reject: {found}"

    def test_objects_are_closed(self):
        assert not list(open_objects(gemini_schema(RAW)))

    def test_const_becomes_a_single_value_enum(self):
        # Gemini has no `const`; a one-value enum constrains it identically.
        type_schema = gemini_schema(RAW)["$defs"]["GetRecords"]["properties"]["type"]
        assert type_schema["enum"] == ["GetRecords"]
        assert "const" not in type_schema

    def test_defaults_and_discriminator_are_dropped(self):
        found = {k for _, k in keywords(gemini_schema(RAW)) if k in {"default", "discriminator"}}
        assert not found, found

    def test_the_union_of_element_types_is_intact(self):
        # Counted from the IR rather than hard-coded, so adding an element type
        # cannot make this test pass by describing the old union.
        from typing import get_args

        from flowtool.ir import Element

        expected = len(get_args(get_args(Element)[0]))
        elements = gemini_schema(RAW)["properties"]["elements"]["items"]
        branches = elements.get("anyOf") or elements.get("oneOf")
        assert branches and len(branches) == expected, "lost element types from the union"


class TestShrinkingForGoodReasons:
    """
    The anti-shrink guard exists because a model "repaired" an unreachable
    element by deleting it. But a flow shrinks for legitimate reasons too - the
    user asked for a step to go, two updates were merged - and rejecting every
    shrink turned a valid answer into a wasted repair round with an instruction
    to restore what the user had asked to remove.

    The rule that separates them: only elements the previous error actually
    named are suspicious.
    """

    SMALLER = _with(elements=[
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ])

    def test_a_removal_asked_for_is_accepted_first_try(self):
        """No previous attempt to compare against, so nothing is suspicious."""
        provider = ScriptedProvider(VALID, self.SMALLER)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        after = generator.refine(first, "drop the account lookup")
        assert after.repairs == 0
        assert [e.name for e in after.flow.elements] == ["Get_Account"]

    def test_shrinking_past_an_unrelated_error_is_not_evasion(self):
        """
        Attempt 1 fails on a dangling reference to something else entirely;
        attempt 2 fixes it and also drops an element nobody complained about.
        That is the requested change landing, not the error being dodged.
        """
        two_elements = _with(elements=[
            {"type": "GetRecords", "name": "Get_Account", "label": "G",
             "object": "Account", "next": "Nowhere"},
            {"type": "GetRecords", "name": "Get_Contact", "label": "C",
             "object": "Contact", "next": None},
        ])
        provider = ScriptedProvider(two_elements, self.SMALLER)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1, "one real repair, not one wasted on the guard"
        assert [e.name for e in result.flow.elements] == ["Get_Account"]

    def test_deleting_the_element_the_error_named_is_still_caught(self):
        """The original failure must stay caught: the error names Get_Account."""
        provider = ScriptedProvider(DANGLING, _with(elements=[]), VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 2
        complaint = provider.calls[2][-1].content
        assert "Get_Account" in complaint
        assert "not a fix" in complaint

    def test_the_complaint_says_the_error_named_them(self):
        provider = ScriptedProvider(DANGLING, _with(elements=[]), VALID)
        FlowGenerator(provider).generate("...")
        complaint = provider.calls[2][-1].content
        assert "names them" in complaint, (
            "the model must be told why this deletion is different from a "
            "legitimate one"
        )
