"""
The planner's job: turn one request into an ordered list of typed steps, then
run each through the matching generator. Plan/PlanStep pin down what a valid
plan looks like; execute_plan pins down that running one actually drives the
Phase 1-3 generators end to end, not just that the plan itself validates.
"""

import threading
import time

import pytest
from pydantic import ValidationError

from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomField, CustomObject
from flowtool.llm import APEX_SYSTEM_PROMPT, FIELD_SYSTEM_PROMPT, OBJECT_SYSTEM_PROMPT
from flowtool.planner import (
    Plan, PlanStep, PlannerGenerator, execute_plan, refine_step, repair_step,
)
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_llm import VALID as VALID_FLOW, ScriptedProvider, TypedScriptedProvider


def _step(**overrides):
    defaults = dict(artifact_type="flow", name="Step", brief="do it")
    defaults.update(overrides)
    return PlanStep(**defaults)


class TestPlanStep:
    def test_valid_shape_is_accepted(self):
        assert _step().artifact_type == "flow"

    def test_unknown_artifact_type_is_rejected(self):
        with pytest.raises(ValidationError, match="must be one of"):
            _step(artifact_type="trigger")

    def test_self_dependency_is_rejected(self):
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            _step(name="A", depends_on=["A"])


class TestPlan:
    def test_single_step_plan_is_valid(self):
        # The Flow-only request stays a one-step plan - see planner.py's
        # module docstring for why that matters.
        plan = Plan(steps=[_step()])
        assert len(plan.steps) == 1

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate step names"):
            Plan(steps=[_step(name="A"), _step(name="A")])

    def test_dependency_on_unknown_step_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown step"):
            Plan(steps=[_step(name="A", depends_on=["Ghost"])])

    def test_ordered_respects_dependencies(self):
        plan = Plan(steps=[
            _step(name="Field", artifact_type="field", depends_on=["Object"]),
            _step(name="Object", artifact_type="object"),
        ])
        assert [s.name for s in plan.ordered()] == ["Object", "Field"]

    def test_ordered_is_stable_when_nothing_depends_on_anything(self):
        plan = Plan(steps=[_step(name="A"), _step(name="B")])
        assert [s.name for s in plan.ordered()] == ["A", "B"]

    def test_diamond_dependencies_resolve(self):
        plan = Plan(steps=[
            _step(name="Apex", artifact_type="apex", depends_on=["FieldA", "FieldB"]),
            _step(name="FieldA", artifact_type="field", depends_on=["Object"]),
            _step(name="FieldB", artifact_type="field", depends_on=["Object"]),
            _step(name="Object", artifact_type="object"),
        ])
        order = [s.name for s in plan.ordered()]
        assert order.index("Object") < order.index("FieldA")
        assert order.index("Object") < order.index("FieldB")
        assert order.index("FieldA") < order.index("Apex")
        assert order.index("FieldB") < order.index("Apex")


class TestPlannerGenerator:
    def test_single_flow_request_is_a_one_step_plan(self):
        payload = {
            "steps": [
                {"artifact_type": "flow", "name": "Won Deal Flow",
                 "brief": "when a deal is won, get the account"},
            ]
        }
        provider = ScriptedProvider(payload)
        result = PlannerGenerator(provider).generate("when a deal is won, get the account")
        assert result.repairs == 0
        assert isinstance(result.value, Plan)
        assert len(result.value.steps) == 1

    def test_invalid_plan_is_repaired(self):
        bad = {"steps": [{"artifact_type": "trigger", "name": "X", "brief": "x"}]}
        good = {"steps": [{"artifact_type": "flow", "name": "X", "brief": "x"}]}
        provider = ScriptedProvider(bad, good)
        result = PlannerGenerator(provider).generate("...")
        assert result.repairs == 1

        complaint = provider.calls[1][-1].content
        assert "failed validation" in complaint


class TestExecutePlan:
    def test_single_flow_step_runs_the_flow_generator(self):
        plan = Plan(steps=[
            PlanStep(artifact_type="flow", name="Flow", brief="build it"),
        ])
        provider = ScriptedProvider(VALID_FLOW)
        results = execute_plan(provider, plan)

        assert len(results) == 1
        assert isinstance(results[0].value, Flow)
        assert results[0].value.api_name == "GC_Won_Deal_Flow"

    def test_object_field_apex_bundle_runs_each_matching_generator(self):
        plan = Plan(steps=[
            PlanStep(artifact_type="object", name="Object", brief="an Invoice object"),
            PlanStep(artifact_type="field", name="Field", brief="an Amount field",
                      depends_on=["Object"]),
            PlanStep(artifact_type="apex", name="Apex", brief="a helper class"),
        ])
        # Object and Apex share a layer (neither depends on anything) and now
        # run concurrently - see execute_plan's docstring - so which one's
        # thread calls the provider first is not guaranteed. A plain
        # ScriptedProvider's FIFO queue would then risk handing Object's
        # thread the Apex payload or vice versa; TypedScriptedProvider routes
        # by which system prompt asked (each artifact type's is a distinct
        # constant), so this stays deterministic regardless of thread timing.
        provider = TypedScriptedProvider(
            object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX,
        )
        results = execute_plan(provider, plan)

        by_name = {r.step.name: r for r in results}
        assert isinstance(by_name["Object"].value, CustomObject)
        assert isinstance(by_name["Field"].value, CustomField)
        assert isinstance(by_name["Apex"].value, ApexClass)
        # Returned in the plan's original order, not execution order.
        assert [r.step.name for r in results] == ["Object", "Field", "Apex"]

    def test_dependency_order_is_honoured_even_when_listed_out_of_order(self):
        plan = Plan(steps=[
            PlanStep(artifact_type="field", name="Field", brief="an Amount field",
                      depends_on=["Object"]),
            PlanStep(artifact_type="object", name="Object", brief="an Invoice object"),
        ])
        provider = ScriptedProvider(VALID_OBJECT, VALID_FIELD)
        results = execute_plan(provider, plan)

        # Two payloads were queued in dependency order (Object, then Field);
        # if execute_plan ran the listed order instead, the Field generator
        # would have been fed the Object's payload and failed to validate.
        by_name = {r.step.name: r for r in results}
        assert isinstance(by_name["Object"].value, CustomObject)
        assert isinstance(by_name["Field"].value, CustomField)

    def test_independent_steps_actually_run_concurrently(self, monkeypatch):
        # Not just "safe under concurrency" (the tests above) - proves the
        # wall-clock benefit is real: three independent steps, each with an
        # artificial delay, finish in about one delay's worth of time, not
        # three, if and only if they're genuinely running in parallel threads
        # rather than one after another.
        plan = Plan(steps=[
            PlanStep(artifact_type="object", name="A", brief="a"),
            PlanStep(artifact_type="field", name="B", brief="b"),
            PlanStep(artifact_type="apex", name="C", brief="c"),
        ])
        provider = TypedScriptedProvider(
            object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX,
        )
        delay = 0.15

        real_complete_json = provider.complete_json

        def slow_complete_json(system, messages, schema):
            time.sleep(delay)
            return real_complete_json(system, messages, schema)

        monkeypatch.setattr(provider, "complete_json", slow_complete_json)

        started = time.monotonic()
        execute_plan(provider, plan)
        elapsed = time.monotonic() - started

        # Sequential would take ~3x delay; concurrent, ~1x. The threshold
        # sits well clear of both to absorb scheduling jitter without being
        # able to pass a sequential run by accident.
        assert elapsed < delay * 2, (
            f"took {elapsed:.3f}s for 3 steps at {delay}s each - "
            "looks sequential, not concurrent"
        )

    def test_dependent_step_waits_for_its_whole_layer_to_finish(self, monkeypatch):
        # B and C are independent (layer 0); A depends on both (layer 1). A
        # must not start until the slower of B/C is actually done, even
        # though the other one finished earlier - a layer is a barrier, not
        # "start as soon as your own deps happen to be done" per step.
        plan = Plan(steps=[
            PlanStep(artifact_type="apex", name="A", brief="a", depends_on=["B", "C"]),
            PlanStep(artifact_type="object", name="B", brief="b"),
            PlanStep(artifact_type="field", name="C", brief="c"),
        ])
        provider = TypedScriptedProvider(
            object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX,
        )
        real_complete_json = provider.complete_json
        finished_layer_0 = threading.Event()
        b_done = {"value": False}
        c_done = {"value": False}

        def tracking_complete_json(system, messages, schema):
            if system == OBJECT_SYSTEM_PROMPT:
                time.sleep(0.05)
                b_done["value"] = True
            elif system == FIELD_SYSTEM_PROMPT:
                time.sleep(0.15)
                c_done["value"] = True
            elif system == APEX_SYSTEM_PROMPT:
                # A's own call - by the time this runs, both of layer 0 must
                # already be marked done.
                assert b_done["value"] and c_done["value"]
                finished_layer_0.set()
            return real_complete_json(system, messages, schema)

        monkeypatch.setattr(provider, "complete_json", tracking_complete_json)
        execute_plan(provider, plan)
        assert finished_layer_0.is_set()


class TestRepairStep:
    def test_flow_step_is_repaired_from_salesforce_errors(self):
        plan = Plan(steps=[PlanStep(artifact_type="flow", name="Flow", brief="build it")])
        provider = ScriptedProvider(VALID_FLOW)
        first = execute_plan(provider, plan)[0]

        provider.payloads.append(VALID_FLOW)
        repaired = repair_step(
            provider, first,
            ["[Error] Mark_Hot (Update Records) - You can't use the sObjectInputReference field"],
        )
        assert isinstance(repaired.value, Flow)

        instruction = provider.calls[-1][-1].content
        assert "Salesforce rejected" in instruction
        assert "sObjectInputReference" in instruction

    def test_field_step_is_repaired_via_the_inherited_base_method(self):
        # CustomFieldGenerator has no repair_from_salesforce of its own - this
        # pins down that the one it inherits from IRGenerator actually works
        # end to end, not just that it exists.
        plan = Plan(steps=[
            PlanStep(artifact_type="field", name="Field", brief="an Amount field"),
        ])
        provider = ScriptedProvider(VALID_FIELD)
        first = execute_plan(provider, plan)[0]

        provider.payloads.append(VALID_FIELD)
        repaired = repair_step(
            provider, first, ["Field does not exist: Amount__c on Invoice__c"],
        )
        assert isinstance(repaired.value, CustomField)
        assert repaired.value.api_name == "Amount__c"

        instruction = provider.calls[-1][-1].content
        assert "Salesforce rejected" in instruction
        assert "Amount__c" in instruction

    def test_repair_continues_the_original_conversation(self):
        plan = Plan(steps=[
            PlanStep(artifact_type="object", name="Object", brief="an Invoice object"),
        ])
        provider = ScriptedProvider(VALID_OBJECT)
        first = execute_plan(provider, plan)[0]

        provider.payloads.append(VALID_OBJECT)
        repair_step(provider, first, ["some failure"])

        second_call = provider.calls[-1]
        assert second_call[0].content == "an Invoice object", "lost the original brief"


class TestRefineStep:
    def test_field_step_is_refined_with_a_proactive_instruction(self):
        # The "before validating" sibling to TestRepairStep - same
        # conversation continuation, but the instruction is a person's
        # request, not a Salesforce failure.
        plan = Plan(steps=[
            PlanStep(artifact_type="field", name="Field", brief="an Amount field"),
        ])
        provider = ScriptedProvider(VALID_FIELD)
        first = execute_plan(provider, plan)[0]

        provider.payloads.append(VALID_FIELD)
        refined = refine_step(provider, first, "make it a Currency field instead")
        assert isinstance(refined.value, CustomField)

        instruction = provider.calls[-1][-1].content
        assert instruction == "make it a Currency field instead"

    def test_refine_continues_the_original_conversation(self):
        plan = Plan(steps=[
            PlanStep(artifact_type="object", name="Object", brief="an Invoice object"),
        ])
        provider = ScriptedProvider(VALID_OBJECT)
        first = execute_plan(provider, plan)[0]

        provider.payloads.append(VALID_OBJECT)
        refine_step(provider, first, "rename the plural label")

        second_call = provider.calls[-1]
        assert second_call[0].content == "an Invoice object", "lost the original brief"
