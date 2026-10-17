"""portal_agent generators."""

import pytest

from flowtool.portal_agent import (
    AgentAction, AgentTopic, agent_files, case_action_apex, function_xml, plugin_xml,
)

ACTION = AgentAction("Create_Support_Case", "Create Support Case", "Creates a support case for the person chatting", "PortalCaseAction")
TOPIC = AgentTopic("Support_Requests", "Support Requests", "Help customers report problems.", "Handles support requests.",
                   actions=["Create_Support_Case"], instructions=["Ask for a subject first.", "Never promise a fix date."])


def test_function_xml_for_apex_and_flow():
    assert "<invocationTargetType>apex</invocationTargetType>" in function_xml(ACTION)
    flow = AgentAction("Look_Up_Order", "Look Up Order", "Finds an order", "Order_Lookup", target_type="flow", confirm_first=False)
    xml = function_xml(flow)
    assert "<invocationTargetType>flow</invocationTargetType>" in xml
    assert "<isConfirmationRequired>false</isConfirmationRequired>" in xml


def test_actions_that_write_data_ask_for_confirmation_by_default():
    assert "<isConfirmationRequired>true</isConfirmationRequired>" in function_xml(ACTION)


def test_plugin_lists_functions_and_numbers_instructions():
    xml = plugin_xml(TOPIC)
    assert "<functionName>Create_Support_Case</functionName>" in xml
    assert "<developerName>instruction_0</developerName>" in xml and "<developerName>instruction_1</developerName>" in xml
    assert "<pluginType>Topic</pluginType>" in xml


def test_text_is_escaped():
    t = AgentTopic("T", "T", "a < b & c", "d", instructions=["<script>"])
    xml = plugin_xml(t)
    assert "a &lt; b &amp; c" in xml and "&lt;script&gt;" in xml


def test_action_needs_a_description():
    with pytest.raises(ValueError):
        AgentAction("A", "A", "  ", "Target")


@pytest.mark.parametrize("bad", ["1Bad", "has space", "trailing_", "dou__ble", ""])
def test_names_are_validated(bad):
    with pytest.raises(ValueError):
        AgentAction(bad, "L", "d", "Target")


def test_unknown_action_in_a_topic_is_rejected():
    with pytest.raises(ValueError, match="unknown action"):
        agent_files([ACTION], [AgentTopic("T", "T", "scope", "d", actions=["Nope"])])


def test_agent_files_layout_and_types():
    files, types = agent_files([ACTION], [TOPIC])
    assert set(files) == {"genAiFunctions/Create_Support_Case.genAiFunction", "genAiPlugins/Support_Requests.genAiPlugin"}
    assert types == {"GenAiFunction": ["Create_Support_Case"], "GenAiPlugin": ["Support_Requests"]}


def test_case_action_apex_has_one_invocable_method():
    src = case_action_apex()
    assert src.count("@InvocableMethod") == 1 and "class PortalCaseAction" in src
    assert src.count("{") == src.count("}")


# --- Agent Script ------------------------------------------------------------

from flowtool.portal_agent import (  # noqa: E402
    AgentScriptSpec, Param, ScriptAction, ScriptTopic, agent_script, authoring_bundle_files,
)


def script_spec(**kw):
    return AgentScriptSpec(
        api_name="Portal_Agent", label="Portal Agent", description="d", agent_user="agent@example.com",
        topics=[ScriptTopic(
            "support_requests", "Support Requests", "Report a problem", "Ask for a subject.\nThen create the case.",
            actions=[ScriptAction("create_support_case", "Creates a case", "PortalCaseAction",
                                  inputs=[Param("subject", "Short summary")], outputs=[Param("caseNumber", "The number")])],
        )],
        **kw,
    )


def test_script_has_the_sections_the_compiler_expects():
    text = agent_script(script_spec())
    for needle in ("system:", "config:", "variables:", "language:", "start_agent agent_router:",
                   "subagent support_requests:", "subagent escalation:", "subagent off_topic:"):
        assert needle in text, needle
    assert 'default_agent_user: "agent@example.com"' in text


def test_router_transitions_to_each_topic():
    assert "go_to_support_requests: @utils.transition to @subagent.support_requests" in agent_script(script_spec())


def test_action_targets_use_the_apex_or_flow_scheme():
    text = agent_script(script_spec())
    assert 'target: "apex://PortalCaseAction"' in text
    flow = ScriptAction("look_up", "Finds an order", "Order_Lookup", target_type="flow")
    spec = script_spec()
    spec.topics[0].actions.append(flow)
    assert 'target: "flow://Order_Lookup"' in agent_script(spec)


def test_inputs_are_passed_from_the_conversation():
    text = agent_script(script_spec())
    assert "use_create_support_case: @actions.create_support_case" in text
    assert "with subject = ..." in text


def test_multiline_instructions_stay_inside_one_pipe_block():
    text = agent_script(script_spec())
    assert "            | Ask for a subject.\n              Then create the case." in text


def test_quotes_in_text_cannot_break_the_script():
    spec = script_spec()
    spec.welcome = 'Say "hi"\nnow'
    text = agent_script(spec)
    assert 'welcome: "Say \\"hi\\" now"' in text


def test_bundle_files_layout():
    files = authoring_bundle_files(script_spec())
    assert set(files) == {"aiAuthoringBundles/Portal_Agent/Portal_Agent.agent",
                          "aiAuthoringBundles/Portal_Agent/Portal_Agent.bundle-meta.xml"}
    assert "<bundleType>AGENT</bundleType>" in files["aiAuthoringBundles/Portal_Agent/Portal_Agent.bundle-meta.xml"]


def test_names_validated_in_script_objects():
    with pytest.raises(ValueError):
        ScriptTopic("bad name", "L", "d", "i")
    with pytest.raises(ValueError):
        Param("1x", "d")
