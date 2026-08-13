"""
Flow Orchestrator: a chain of stages, each running a sequence of steps.

processType 'Orchestrator' is its own canvas in Flow Builder - stages instead
of the usual elements, connected the same way any element connects to the
next one. Confirmed against a real dev org's checkOnly validation, and it
took a real flow, built in Flow Builder and retrieved back, to find the one
fact that could not be guessed from the schema: the actionType a stage step
needs is not a normal ActionCall type. Every ordinary one was tried against
the org and refused with the same message:

    You can't use the <X> action type in flows with the Flow Orchestration
    process type.

tried for X = flow, apex, emailSimple, submit, chatterPost. The real values
are a pair of orchestration-only literals, `stepBackground`/`stepInteractive`,
which this IR derives from `step_subtype` rather than asking for twice.

Approval steps, MuleSoft steps, and evaluating entry/exit criteria with a
dedicated EvaluationFlow (confirmed real - the referenced flow must be
process_type 'EvaluationFlow' with a Boolean output variable literally named
`isOrchestrationConditionMet`) are out of scope here.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Condition,
    Flow,
    OrchestratedStage,
    StageStep,
    StageStepAssignee,
    Start,
    Value,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate


def stage_step(**kwargs) -> StageStep:
    fields = dict(
        name="Do_Work", label="Do Work", step_subtype="BackgroundStep",
        action_name="Some_Autolaunched_Flow",
    )
    fields.update(kwargs)
    return StageStep(**fields)


def stage(*steps, **kwargs) -> OrchestratedStage:
    fields = dict(
        name="Stage_One", label="Stage One",
        stage_steps=list(steps) or [stage_step()],
    )
    fields.update(kwargs)
    return OrchestratedStage(**fields)


def flow(*stages, **kwargs) -> Flow:
    stage_list = list(stages) or [stage()]
    fields = dict(
        api_name="Orchestrator_Flow", label="Orchestrator Flow",
        process_type="Orchestrator",
        start=Start(next=stage_list[0].name),
        elements=stage_list,
    )
    fields.update(kwargs)
    return Flow(**fields)


def survives(f: Flow) -> bool:
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    return before == after


class TestRoundTrip:
    def test_a_background_step_survives(self):
        assert survives(flow(stage(stage_step())))

    def test_an_interactive_step_survives(self):
        assert survives(flow(stage(stage_step(
            name="Ask_User", step_subtype="InteractiveStep",
            action_name="Some_Screen_Flow",
            assignees=[StageStepAssignee(assignee=Value(element_reference="$User.Id"))],
        ))))

    def test_a_group_assignee_survives(self):
        assert survives(flow(stage(stage_step(
            name="Ask_User", step_subtype="InteractiveStep",
            action_name="Some_Screen_Flow",
            assignees=[StageStepAssignee(
                assignee=Value(string_value="00G000000000001"), assignee_type="Group",
            )],
        ))))

    def test_entry_and_exit_conditions_survive(self):
        condition = Condition(left="$Record.Amount", operator="GreaterThan",
                               right=Value(number_value=100))
        assert survives(flow(stage(stage_step(
            entry_conditions=[condition], exit_conditions=[condition],
        ))))

    def test_a_stage_exit_condition_survives(self):
        condition = Condition(left="$Record.Amount", operator="GreaterThan",
                               right=Value(number_value=100))
        assert survives(flow(stage(exit_conditions=[condition])))

    def test_two_stages_chained_survive(self):
        first = stage(next="Stage_Two")
        second = stage(stage_step(name="Ask_User", step_subtype="InteractiveStep",
                                   action_name="Some_Screen_Flow",
                                   assignees=[StageStepAssignee(
                                       assignee=Value(element_reference="$User.Id"))]),
                        name="Stage_Two", label="Stage Two")
        assert survives(flow(first, second))

    def test_multiple_steps_in_one_stage_survive(self):
        assert survives(flow(stage(
            stage_step(name="First"),
            stage_step(name="Second"),
        )))

    def test_a_record_triggered_orchestrator_survives(self):
        assert survives(flow(stage(), start=Start(
            next="Stage_One", object="Lead", record_trigger_type="CreateAndUpdate",
            trigger_type="RecordAfterSave",
        )))


class TestGeneratedXml:
    def test_a_background_step_writes_the_orchestration_action_type(self):
        xml = generate(flow(stage(stage_step())))
        assert "<actionType>stepBackground</actionType>" in xml
        assert "<actionType>flow</actionType>" not in xml

    def test_an_interactive_step_writes_the_orchestration_action_type(self):
        xml = generate(flow(stage(stage_step(
            step_subtype="InteractiveStep", action_name="Some_Screen_Flow",
            assignees=[StageStepAssignee(assignee=Value(element_reference="$User.Id"))],
        ))))
        assert "<actionType>stepInteractive</actionType>" in xml

    def test_a_background_step_writes_no_assignees(self):
        xml = generate(flow(stage(stage_step())))
        assert "<assignees>" not in xml


class TestTheIrChecksWhatTheOrgDoesNot:
    def test_an_interactive_step_needs_an_assignee(self):
        with pytest.raises(ValidationError, match="assignee"):
            stage_step(step_subtype="InteractiveStep", action_name="Some_Screen_Flow")

    def test_a_background_step_refuses_an_assignee(self):
        with pytest.raises(ValidationError, match="Background Step"):
            stage_step(assignees=[
                StageStepAssignee(assignee=Value(element_reference="$User.Id"))
            ])

    def test_a_stage_needs_at_least_one_step(self):
        with pytest.raises(ValidationError):
            OrchestratedStage(name="Stage_One", label="Stage One", stage_steps=[])

    def test_duplicate_step_names_in_one_stage_are_refused(self):
        with pytest.raises(ValidationError, match="duplicate"):
            stage(stage_step(name="X"), stage_step(name="X"))

    def test_process_type_orchestrator_needs_at_least_one_stage(self):
        with pytest.raises(ValidationError, match="no stages"):
            Flow(api_name="Empty_Orchestrator", label="Empty Orchestrator",
                 process_type="Orchestrator", start=Start(), elements=[])

    def test_a_stage_cannot_appear_outside_an_orchestrator(self):
        with pytest.raises(ValidationError, match="Orchestrated Stage"):
            flow(stage(), process_type="AutoLaunchedFlow", start=Start(next="Stage_One"))

    def test_an_orchestrator_cannot_mix_stages_with_regular_elements(self):
        from flowtool.ir import Assignment, AssignmentItem

        assignment = Assignment(
            name="Set_Something", label="Set Something",
            items=[AssignmentItem(to_reference="$Record.Name",
                                   operator="Assign", value=Value(string_value="x"))],
        )
        with pytest.raises(ValidationError, match="different kind of element"):
            flow(stage(), elements=[stage(), assignment])


class TestParsing:
    def test_an_unknown_child_of_a_stage_step_is_refused(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>Orchestrator</processType><status>Draft</status>"
            "<orchestratedStages><name>Stage_One</name><label>Stage One</label>"
            "<stageSteps><name>X1</name><actionName>Y</actionName>"
            "<actionType>stepBackground</actionType><stepSubtype>BackgroundStep</stepSubtype>"
            "<somethingMadeUp>x</somethingMadeUp></stageSteps>"
            "</orchestratedStages>"
            "<start><connector><targetReference>Stage_One</targetReference></connector></start>"
            "</Flow>"
        )
        with pytest.raises(UnsupportedFlow, match="somethingMadeUp"):
            parse_flow(xml)

    def test_an_unknown_process_type_is_refused(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>Survey</processType><status>Draft</status>"
            "<start></start></Flow>"
        )
        with pytest.raises(UnsupportedFlow, match="process type Survey"):
            parse_flow(xml)


class TestWhatTheUserSees:
    def test_the_diagram_names_the_step(self):
        assert "Do Work" in to_mermaid(flow(stage(stage_step())))

    def test_the_markdown_shows_the_action(self):
        doc = to_markdown(flow(stage(stage_step())))
        assert "Some_Autolaunched_Flow" in doc

    def test_the_markdown_shows_who_is_assigned(self):
        doc = to_markdown(flow(stage(stage_step(
            step_subtype="InteractiveStep", action_name="Some_Screen_Flow",
            assignees=[StageStepAssignee(assignee=Value(element_reference="$User.Id"))],
        ))))
        assert "$User.Id" in doc
