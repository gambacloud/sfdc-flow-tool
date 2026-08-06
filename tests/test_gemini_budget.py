"""
Gemini caps how large a response schema may be, and stage 2 crossed it.

The cap is undocumented and the API says only "Request contains an invalid
argument" - it names neither the schema nor the size. It was found by bisection
against the real API, and what it counts is the schema with every $ref inlined:
a 23,000-character schema with 216 expanded properties is accepted while an
18,000-character one with 221 is rejected. So neither bytes nor $def count nor
nesting depth is the thing to measure, and the counting rule is pinned here.

The response to being over the cap is to send the schema as prompt text rather
than to prune it. Pruning would make whatever was pruned unrepresentable, and
refining an imported flow would then delete exactly those parts on the way back
out - the silent-drop bug, one layer up.
"""

import json

import pytest

from flowtool.ir import Flow
from flowtool.llm import (
    GEMINI_PROPERTY_BUDGET,
    _unfenced,
    expanded_property_count,
    gemini_schema,
)


class TestCountingRule:
    def test_a_flat_object_counts_its_properties(self):
        assert expanded_property_count({
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        }) == 2

    def test_a_shared_definition_is_counted_once_per_reference(self):
        """
        This is the whole point. Six references to a six-property definition
        cost thirty-six, not six - which is why the byte size is no guide.
        """
        schema = {
            "type": "object",
            "properties": {
                "one": {"$ref": "#/$defs/Pair"},
                "two": {"$ref": "#/$defs/Pair"},
                "three": {"$ref": "#/$defs/Pair"},
            },
            "$defs": {"Pair": {
                "type": "object",
                "properties": {"x": {"type": "string"}, "y": {"type": "string"}},
            }},
        }
        assert expanded_property_count(schema) == 3 + 3 * 2

    def test_definitions_nobody_references_cost_nothing(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "$defs": {"Unused": {
                "type": "object",
                "properties": {f"p{i}": {"type": "string"} for i in range(50)},
            }},
        }
        assert expanded_property_count(schema) == 1

    def test_it_reaches_through_arrays_and_unions(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"$ref": "#/$defs/Leaf"}},
                "either": {"anyOf": [{"$ref": "#/$defs/Leaf"}, {"type": "null"}]},
            },
            "$defs": {"Leaf": {"type": "object", "properties": {"x": {"type": "string"}}}},
        }
        assert expanded_property_count(schema) == 2 + 1 + 1

    def test_a_recursive_schema_terminates(self):
        schema = {
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/Node"}},
            "$defs": {"Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}, "name": {"type": "string"}},
            }},
        }
        assert expanded_property_count(schema) == 3


class TestTheRealSchema:
    def test_the_budget_is_below_the_measured_limit(self):
        # 220 accepted, 221 rejected, measured against the API.
        assert GEMINI_PROPERTY_BUDGET < 221, "must leave room under the real cap"

    def test_the_flow_schema_is_measured_not_guessed(self):
        """
        Whether the IR currently fits is a fact about the IR, and it changes as
        element types are added. This records what it is rather than asserting
        it fits, so growth is visible instead of surprising.
        """
        cost = expanded_property_count(gemini_schema(Flow.model_json_schema()))
        assert cost > 0
        # If this trips, the IR shrank enough to be schema-constrained again -
        # good news, and worth noticing.
        assert cost > GEMINI_PROPERTY_BUDGET, (
            f"the IR now costs {cost}, within the {GEMINI_PROPERTY_BUDGET} budget: "
            "Gemini can be schema-constrained again"
        )

    def test_the_element_union_is_what_dominates(self):
        """
        180 of the cost is the ten element types, so every new one moves this.
        Named here so the next person adding an element knows what it costs.
        """
        schema = gemini_schema(Flow.model_json_schema())
        defs = schema["$defs"]
        elements = schema["properties"]["elements"]
        one_type = expanded_property_count(
            {"properties": {"x": {"$ref": "#/$defs/Screen"}}, "$defs": defs}
        )
        whole_union = expanded_property_count(
            {"properties": {"x": elements}, "$defs": defs}
        )
        assert whole_union > one_type * 5, "the union is the dominant cost"


class TestFallbackShape:
    """
    The provider is not exercised here - it needs a network - but the decision
    it makes is pure arithmetic and worth pinning.
    """

    def test_over_budget_is_detected_for_the_real_schema(self):
        cost = expanded_property_count(gemini_schema(Flow.model_json_schema()))
        assert cost > GEMINI_PROPERTY_BUDGET

    def test_a_small_schema_stays_constrained(self):
        small = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert expanded_property_count(small) <= GEMINI_PROPERTY_BUDGET


class TestUnfencing:
    def test_bare_json_is_untouched(self):
        assert _unfenced('{"a": 1}') == '{"a": 1}'

    @pytest.mark.parametrize("fence", ["```json", "```"])
    def test_a_fence_is_stripped(self, fence):
        assert json.loads(_unfenced(f'{fence}\n{{"a": 1}}\n```')) == {"a": 1}

    def test_whitespace_around_a_fence_is_tolerated(self):
        assert json.loads(_unfenced('\n  ```json\n{"a": 1}\n```  \n')) == {"a": 1}

    def test_a_fence_with_no_body_does_not_crash(self):
        assert _unfenced("```") == ""


class TestModelOrdering:
    """
    The picker's first entries are the ones anyone will actually read, so the
    ordering is part of the feature rather than cosmetics.
    """

    def test_versions_compare_as_numbers(self):
        from flowtool.llm import _descending

        names = ["gemini-3-flash-preview", "gemini-3.5-flash", "gemini-3.6-flash",
                 "gemini-3.1-pro-preview", "gemini-2.5-pro"]
        assert sorted(names, key=_descending) == [
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
        ]

    def test_a_decimal_version_outranks_its_bare_major(self):
        # Splitting on digits alone made "3.5" sort below "3-preview", because
        # "-" precedes "." in ASCII.
        from flowtool.llm import _descending

        assert _descending("gemini-3.5-flash") < _descending("gemini-3-flash-preview")

    def test_non_text_models_are_named_not_guessed(self):
        from flowtool.llm import _GEMINI_NOT_TEXT

        for word in ("embedding", "image", "tts", "veo", "robotics", "computer-use"):
            assert word in _GEMINI_NOT_TEXT
