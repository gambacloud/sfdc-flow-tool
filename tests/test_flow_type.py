"""Per-flow-type narrowing of the Flow prompt and schema."""

import pytest

from flowtool import llm
from flowtool.ir import Flow
from flowtool.llm import FLOW_TYPES, FlowGenerator, flow_schema, flow_system_prompt
from flowtool.planner import Plan, PlanStep, _generator_for


def _elements(schema):
    """Element class names the schema still offers in Flow.elements."""
    items = schema["properties"]["elements"]["items"]
    members = items.get("oneOf") or items.get("anyOf") or []
    return {m["$ref"].split("/")[-1] for m in members}


class TestFlowSchema:
    def test_unknown_or_missing_type_is_the_full_schema(self):
        full = Flow.model_json_schema()
        assert flow_schema(None) == full
        assert flow_schema("nonsense") == full

    def test_autolaunched_drops_screens_and_stages(self):
        schema = flow_schema("autolaunched")
        offered = _elements(schema)
        assert "Screen" not in offered and "OrchestratedStage" not in offered
        assert "RecordCreate" in offered and "Decision" in offered
        assert "ScreenField" not in schema["$defs"]
        assert "choices" not in schema["properties"]

    def test_screen_keeps_screens_but_not_stages(self):
        schema = flow_schema("screen")
        assert "Screen" in _elements(schema)
        assert "OrchestratedStage" not in _elements(schema)
        assert "choices" in schema["properties"]

    def test_every_narrowed_schema_is_smaller_and_self_consistent(self):
        full = len(str(Flow.model_json_schema()))
        for flow_type in FLOW_TYPES:
            schema = flow_schema(flow_type)
            assert len(str(schema)) < full
            # no dangling $ref: every reference points at a kept definition
            found: set = set()
            llm._schema_refs({k: v for k, v in schema.items() if k != "$defs"}, found)
            for definition in schema["$defs"].values():
                llm._schema_refs(definition, found)
            assert found <= set(schema["$defs"])

    def test_the_full_model_still_accepts_what_a_narrowed_schema_hides(self):
        # The narrowed schema is only a hint: validation is against Flow itself.
        flow = Flow.model_validate({
            "api_name": "S", "label": "S", "process_type": "Flow",
            "start": {"next": "Screen1"},
            "elements": [{"type": "Screen", "name": "Screen1", "label": "Screen 1"}],
        })
        assert flow.elements[0].type == "Screen"


class TestFlowSystemPrompt:
    def test_autolaunched_drops_screen_sections_and_keeps_the_rest(self):
        prompt = flow_system_prompt("autolaunched")
        assert "## Screens" not in prompt
        assert "## Options for a picker" not in prompt
        assert "## Action Calls" in prompt and "## How the IR works" in prompt
        assert "AutoLaunchedFlow" in prompt
        assert len(prompt) < len(llm.SYSTEM_PROMPT)

    def test_screen_and_none_are_untouched(self):
        assert flow_system_prompt("screen") == llm.SYSTEM_PROMPT
        assert flow_system_prompt(None) == llm.SYSTEM_PROMPT


class TestPlannerFlowType:
    def test_flow_type_is_dropped_on_non_flow_steps(self):
        step = PlanStep(artifact_type="object", name="O", brief="b", flow_type="screen")
        assert step.flow_type is None

    def test_flow_step_keeps_it(self):
        step = PlanStep(artifact_type="flow", name="F", brief="b", flow_type="autolaunched")
        assert step.flow_type == "autolaunched"

    def test_invalid_flow_type_is_rejected(self):
        with pytest.raises(ValueError):
            PlanStep(artifact_type="flow", name="F", brief="b", flow_type="record_triggered")

    def test_step_generator_is_narrowed_to_the_planned_type(self):
        step = PlanStep(artifact_type="flow", name="F", brief="b", flow_type="autolaunched")
        generator = _generator_for(step, provider=object(), max_repairs=1)
        assert isinstance(generator, FlowGenerator)
        assert "## Screens" not in generator.system_prompt
        assert "Screen" not in _elements(generator._schema)

    def test_step_without_a_type_gets_the_full_generator(self):
        step = PlanStep(artifact_type="flow", name="F", brief="b")
        generator = _generator_for(step, provider=object(), max_repairs=1)
        assert generator.system_prompt == llm.SYSTEM_PROMPT

    def test_plan_round_trips_flow_type(self):
        plan = Plan.model_validate({"steps": [
            {"artifact_type": "flow", "name": "F", "brief": "b", "flow_type": "screen"},
        ]})
        assert plan.steps[0].flow_type == "screen"
