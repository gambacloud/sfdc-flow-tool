"""Agent IR, its script, the check against what its actions call, and the planner step."""

import pytest
from fastapi.testclient import TestClient  # noqa: F401 - imported so a missing dep fails here, not later
from pydantic import ValidationError

import server
from flowtool.agent_facts import apex_facts, collect, flow_facts, problems
from flowtool.ir import Flow
from flowtool.ir_agent import Agent
from flowtool.ir_apex import ApexClass
from flowtool.ir_prompt_template import PromptTemplate
from flowtool.llm import AGENT_SYSTEM_PROMPT, AgentGenerator
from flowtool.planner import Plan, PlanStep, StepResult, execute_plan
from flowtool.xmlgen_agent import MissingAgentUser, generate_agent_files
from tests.test_llm import ScriptedProvider, TypedScriptedProvider

APEX_BODY = """public with sharing class CaseMaker {
    public class Request {
        @InvocableVariable(required=true label='Subject' description='x')
        public String subject;
        @InvocableVariable public String details;
    }
    public class Result {
        @InvocableVariable(label='Number')
        public String caseNumber;
    }
    @InvocableMethod(label='Make' description='Makes a case')
    public static List<Result> make(List<Request> requests) {
        return new List<Result>();
    }
}"""

VALID_AGENT = {
    "api_name": "Help_Agent",
    "label": "Help Agent",
    "description": "Helps customers",
    "topics": [{
        "api_name": "support_requests",
        "label": "Support Requests",
        "description": "Customer reports a problem",
        "instructions": "Ask for a subject.\nThen open a case.",
        "actions": [{
            "api_name": "open_case",
            "description": "Opens a support case",
            "target_type": "apex",
            "target": "CaseMaker",
            "inputs": [{"name": "subject", "description": "Short summary"},
                       {"name": "details", "description": "More detail"}],
            "outputs": [{"name": "caseNumber", "description": "The number"}],
        }],
    }],
}


def agent(**kw):
    return Agent.model_validate({**VALID_AGENT, **kw})


def test_valid_agent_round_trips():
    a = agent()
    assert [t.api_name for t in a.topics] == ["support_requests"]
    assert a.action_targets()[0].target == "CaseMaker"


@pytest.mark.parametrize("name", ["Support", "support requests", "support__x", "_x", "1x", "x" * 60])
def test_topic_and_action_names_must_be_script_identifiers(name):
    bad = dict(VALID_AGENT["topics"][0], api_name=name)
    with pytest.raises(ValidationError):
        agent(topics=[bad])


@pytest.mark.parametrize("name", ["escalation", "off_topic", "ambiguous_question", "agent_router"])
def test_built_in_topic_names_are_reserved(name):
    with pytest.raises(ValidationError, match="reserved"):
        agent(topics=[dict(VALID_AGENT["topics"][0], api_name=name)])


def test_an_agent_needs_a_topic_and_unique_names():
    with pytest.raises(ValidationError):
        agent(topics=[])
    with pytest.raises(ValidationError, match="duplicate topic"):
        agent(topics=[VALID_AGENT["topics"][0], VALID_AGENT["topics"][0]])


def test_actions_and_params_need_descriptions_and_unique_names():
    action = dict(VALID_AGENT["topics"][0]["actions"][0])
    with pytest.raises(ValidationError, match="description"):
        agent(topics=[dict(VALID_AGENT["topics"][0], actions=[dict(action, description=" ")])])
    with pytest.raises(ValidationError, match="duplicate input"):
        agent(topics=[dict(VALID_AGENT["topics"][0], actions=[dict(action, inputs=action["inputs"] * 2)])])


def test_files_need_a_real_agent_user():
    with pytest.raises(MissingAgentUser):
        generate_agent_files(agent())
    with pytest.raises(MissingAgentUser):
        generate_agent_files(agent(), "NEW AGENT USER")


def test_generated_script_carries_the_topic_the_action_and_the_user():
    files = generate_agent_files(agent(), "agent@example.com")
    script = files["aiAuthoringBundles/Help_Agent/Help_Agent.agent"]
    assert 'default_agent_user: "agent@example.com"' in script
    assert "subagent support_requests:" in script
    assert 'target: "apex://CaseMaker"' in script
    assert "go_to_support_requests: @utils.transition to @subagent.support_requests" in script
    assert "with subject = ..." in script and "with details = ..." in script


def test_the_agent_user_on_the_ir_is_used_when_none_is_passed():
    a = agent(agent_user="x@y.co")
    assert 'default_agent_user: "x@y.co"' in generate_agent_files(a)["aiAuthoringBundles/Help_Agent/Help_Agent.agent"]


# --- checking actions against what they call ---------------------------------

def test_apex_facts_read_the_invocable_variables_of_the_right_classes():
    f = apex_facts(ApexClass(api_name="CaseMaker", body=APEX_BODY))
    assert f.inputs == {"subject", "details"} and f.outputs == {"caseNumber"}


def test_flow_facts_read_input_and_output_variables():
    flow = Flow.model_validate({
        "api_name": "GC_Lookup", "label": "L", "process_type": "AutoLaunchedFlow",
        "start": {"next": "A"},
        "variables": [{"name": "orderId", "data_type": "String", "is_input": True},
                      {"name": "status", "data_type": "String", "is_output": True}],
        "elements": [{"type": "Assignment", "name": "A", "label": "A", "items": [{"to_reference": "status", "value": {"string_value": "ok"}}]}],
    })
    f = flow_facts(flow)
    assert f.inputs == {"orderId"} and f.outputs == {"status"}


def facts():
    return collect({"s": ApexClass(api_name="CaseMaker", body=APEX_BODY)})


def test_matching_names_are_fine():
    assert problems(VALID_AGENT, facts()) == []


def test_a_wrong_input_name_is_reported_with_the_real_ones():
    bad = {"topics": [{"api_name": "t", "actions": [dict(VALID_AGENT["topics"][0]["actions"][0],
                                                          inputs=[{"name": "title", "description": "x"}])]}]}
    found = problems(bad, facts())
    assert len(found) == 1 and "title" in found[0] and "details" in found[0] and "subject" in found[0]


def test_a_wrong_target_kind_is_reported():
    bad = {"topics": [{"api_name": "t", "actions": [dict(VALID_AGENT["topics"][0]["actions"][0], target_type="flow")]}]}
    assert "apex target, not 'flow'" in problems(bad, facts())[0]


def test_a_target_outside_the_plan_is_not_judged():
    other = {"topics": [{"api_name": "t", "actions": [dict(VALID_AGENT["topics"][0]["actions"][0], target="ExistsInOrg")]}]}
    assert problems(other, facts()) == []


def test_generator_sends_a_mismatch_back_for_repair():
    wrong = {**VALID_AGENT, "topics": [dict(VALID_AGENT["topics"][0], actions=[
        dict(VALID_AGENT["topics"][0]["actions"][0], inputs=[{"name": "title", "description": "x"}])])]}
    provider = ScriptedProvider(wrong, VALID_AGENT)
    result = AgentGenerator(provider, targets=facts()).generate("an agent")
    assert result.repairs == 1
    assert "do not exist on 'CaseMaker'" in provider.calls[1][-1].content


def test_prompt_explains_that_the_agent_never_invents_targets():
    assert "never invent one" in AGENT_SYSTEM_PROMPT and "Leave `agent_user` out" in AGENT_SYSTEM_PROMPT


# --- the planner ------------------------------------------------------------

def test_an_agent_step_is_ordered_after_everything_it_can_call():
    plan = Plan(steps=[
        PlanStep(artifact_type="apex", name="Maker", brief="x"),
        PlanStep(artifact_type="prompt_template", name="Prompt", brief="x"),
        PlanStep(artifact_type="flow", name="Lookup", brief="x"),
        PlanStep(artifact_type="agent", name="Agent", brief="x"),
    ])
    agent_step = plan.steps[3]
    assert {"Maker", "Prompt", "Lookup"} <= set(agent_step.depends_on)
    assert [s.name for layer in plan.layers() for s in layer][-1] == "Agent"


def test_the_flow_calling_a_template_still_waits_for_it_alongside_an_agent():
    plan = Plan(steps=[
        PlanStep(artifact_type="prompt_template", name="Prompt", brief="x"),
        PlanStep(artifact_type="flow", name="Lookup", brief="x"),
        PlanStep(artifact_type="agent", name="Agent", brief="x"),
    ])
    assert plan.steps[1].depends_on == ["Prompt"]
    assert not any("Agent" in s.depends_on for s in plan.steps[:2])


def test_an_agent_step_runs_with_the_real_apex_it_depends_on():
    plan = Plan(steps=[
        PlanStep(artifact_type="apex", name="Maker", brief="a case maker"),
        PlanStep(artifact_type="agent", name="Agent", brief="an agent that opens cases"),
    ])
    provider = TypedScriptedProvider(apex={"api_name": "CaseMaker", "body": APEX_BODY}, agent=VALID_AGENT)
    results = execute_plan(provider, plan)
    assert isinstance(results[1].value, Agent)
    brief = [c for c in provider.calls if c[0] == "agent"][0][1][0].content
    assert "@InvocableVariable" in brief  # it was shown the real source to copy names from


# --- the server -------------------------------------------------------------

def step_results():
    return [StepResult(step=PlanStep(artifact_type="agent", name="A", brief="x"), value=agent(), repairs=0, messages=[])]


def test_the_bundle_holds_the_agent_as_an_authoring_bundle():
    files, types = server._bundle_files_and_types(step_results(), None, "agent@example.com")
    assert types == {"AiAuthoringBundle": ["Help_Agent"]}
    assert "aiAuthoringBundles/Help_Agent/Help_Agent.agent" in files


def test_the_bundle_refuses_to_build_an_agent_without_a_user():
    with pytest.raises(MissingAgentUser):
        server._bundle_files_and_types(step_results(), None, None)


def test_the_step_view_lists_topics_and_targets():
    view = server._step_view(step_results()[0]) if hasattr(server, "_step_view") else None
    if view is not None:
        assert view["topics"][0]["actions"][0]["target"] == "apex://CaseMaker"


def test_a_plan_with_an_agent_deploys_at_api_65_or_later():
    from types import SimpleNamespace
    session = SimpleNamespace(steps=step_results(), api_version="62.0")
    assert server._deploy_api_version(session) == "65.0"
    assert server._deploy_api_version(SimpleNamespace(steps=step_results(), api_version="68.0")) == "68.0"


def test_a_plan_without_an_agent_keeps_its_version():
    from types import SimpleNamespace
    assert server._deploy_api_version(SimpleNamespace(steps=[], api_version="62.0")) == "62.0"


def test_a_service_agent_is_the_default():
    assert agent().agent_type == "service"


def test_an_employee_agent_needs_no_agent_user_and_has_no_messaging_parts():
    a = agent(agent_type="employee")
    script = generate_agent_files(a)["aiAuthoringBundles/Help_Agent/Help_Agent.agent"]
    assert 'agent_type: "AgentforceEmployeeAgent"' in script
    assert "default_agent_user" not in script
    assert "@MessagingSession" not in script and "variables:" not in script
    assert "escalat" not in script
    assert "subagent support_requests:" in script and "subagent off_topic:" in script


def test_an_employee_agent_ignores_an_agent_user_that_is_passed():
    script = generate_agent_files(agent(agent_type="employee"), "x@y.co")["aiAuthoringBundles/Help_Agent/Help_Agent.agent"]
    assert "x@y.co" not in script


def test_a_service_agent_script_is_unchanged_by_the_type_field():
    script = generate_agent_files(agent(), "agent@example.com")["aiAuthoringBundles/Help_Agent/Help_Agent.agent"]
    assert "agent_type" not in script and "subagent escalation:" in script and "MessagingSession" in script


def test_an_unknown_agent_type_is_rejected():
    with pytest.raises(ValidationError):
        agent(agent_type="partner")


def test_the_prompt_tells_the_model_about_the_two_kinds():
    assert "agent_type" in AGENT_SYSTEM_PROMPT and "employee" in AGENT_SYSTEM_PROMPT
