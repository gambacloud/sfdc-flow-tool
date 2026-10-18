"""Opening an existing Prompt Template from the org: parse it into the IR, refuse what the IR cannot hold."""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.ir_prompt_template import PromptTemplate
from flowtool.prompt_import import UnsupportedTemplate, parse_prompt_template
from flowtool.xmlgen_prompt_template import generate_prompt_template
from tests.test_llm import ScriptedProvider
from tests.test_server import poll

NS = 'xmlns="http://soap.sforce.com/2006/04/metadata"'


def template_xml(*, kind="einstein_gpt__flex", fmt="JSON", schema=None, input_def="primitive://String",
                 content="Score {!$Input:transcript}", extra_version=""):
    schema_el = f"<outputSchema>{schema}</outputSchema>" if schema else ""
    return f"""﻿<?xml version="1.0" encoding="UTF-8"?>
<GenAiPromptTemplate {NS}>
    <activeVersionIdentifier>abc_1</activeVersionIdentifier>
    <description>Audits chats</description>
    <developerName>Chat_Audit</developerName>
    <masterLabel>Chat Audit</masterLabel>
    <templateVersions>
        <content>{content}</content>
        <inputs>
            <apiName>transcript</apiName>
            <definition>{input_def}</definition>
            <description>The text</description>
            <masterLabel>transcript</masterLabel>
            <referenceName>Input:transcript</referenceName>
            <required>true</required>
        </inputs>
        <primaryModel>sfdc_ai__DefaultOpenAIGPT4OmniMini</primaryModel>
        <responseFormat>{fmt}</responseFormat>
        {schema_el}
        <status>Published</status>
        <versionIdentifier>abc_1</versionIdentifier>
    </templateVersions>
    {extra_version}
    <type>{kind}</type>
    <visibility>Global</visibility>
</GenAiPromptTemplate>"""


SCHEMA = '{"type":"object","properties":{"score":{"type":"number"}},"required":["score"]}'


def test_a_flex_template_becomes_the_same_ir_the_tool_generates():
    t = parse_prompt_template(template_xml(schema=SCHEMA), "Chat_Audit")
    assert t.api_name == "Chat_Audit" and t.label == "Chat Audit" and t.description == "Audits chats"
    assert t.response_format == "JSON" and t.output_keys() == ["score"]
    assert [(i.name, i.type, i.required) for i in t.inputs] == [("transcript", "String", True)]
    assert t.output_schema_unknown is False


def test_it_round_trips_through_the_generator():
    t = parse_prompt_template(template_xml(schema=SCHEMA), "Chat_Audit")
    again = parse_prompt_template(generate_prompt_template(t), "Chat_Audit")
    assert again == t


def test_a_json_template_without_a_schema_is_opened_and_written_back_without_one():
    t = parse_prompt_template(template_xml(), "Chat_Audit")
    assert t.output_schema_unknown is True and t.output_schema is None
    assert "outputSchema" not in generate_prompt_template(t)


def test_a_new_json_template_still_needs_a_schema():
    with pytest.raises(ValueError, match="output_schema"):
        PromptTemplate(api_name="X", label="X", content="hi", response_format="JSON")


def test_the_active_version_is_the_one_opened():
    older = template_xml(content="old {!$Input:transcript}")
    xml = older.replace("</templateVersions>", "</templateVersions>\n    <templateVersions><content>new</content>"
                        "<primaryModel>m</primaryModel><responseFormat>Text</responseFormat>"
                        "<versionIdentifier>abc_2</versionIdentifier></templateVersions>", 1)
    assert parse_prompt_template(xml, "Chat_Audit").content.startswith("old")  # abc_1 is the active one


@pytest.mark.parametrize("xml, reason", [
    (template_xml(kind="einstein_gpt__salesEmail"), "free-form"),
    (template_xml(input_def="SOBJECT://Account"), "cannot represent"),
    (template_xml(fmt="XML"), "response format"),
    (template_xml(schema="{not json"), "not valid JSON"),
    (template_xml(content="no inputs used"), "never used"),
])
def test_what_the_ir_cannot_hold_is_refused_with_the_reason(xml, reason):
    with pytest.raises(UnsupportedTemplate, match=reason):
        parse_prompt_template(xml, "Chat_Audit")


@pytest.fixture
def client(monkeypatch):
    server.PLAN_SESSIONS.clear()
    monkeypatch.setattr(server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok"))
    with TestClient(server.app) as c:
        yield c


def test_opening_a_template_gives_a_one_step_plan_that_can_be_revised_and_deployed(client, monkeypatch):
    template = parse_prompt_template(template_xml(schema=SCHEMA), "Chat_Audit")

    async def fake_retrieve(url, token, api_name):
        assert api_name == "Chat_Audit"
        return template

    monkeypatch.setattr(server, "_retrieve_prompt_template", fake_retrieve)
    monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: ScriptedProvider())

    started = client.post("/api/import/start", json={"api_name": "Chat_Audit", "kind": "prompt_template"})
    assert started.status_code == 200, started.text
    data = poll(client, "/api/import/status", job_id=started.json()["job_id"]).json()

    assert data["opened_as"] == "plan"
    assert [s["artifact_type"] for s in data["steps"]] == ["prompt_template"]
    assert data["steps"][0]["api_name"] == "Chat_Audit"
    session = server.PLAN_SESSIONS[data["session_id"]]
    assert session.steps[0].messages, "the revise step needs the opened template as its starting point"

    files, types = server._bundle_files_and_types(session.steps, None, None)
    assert types == {"GenAiPromptTemplate": ["Chat_Audit"]}


def test_the_template_list_comes_from_the_orgs_metadata(client, monkeypatch):
    async def fake_list(_client):
        return [{"api_name": "A"}, {"api_name": "B"}]

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(server, "MetadataClient", FakeClient)
    monkeypatch.setattr(server, "list_prompt_templates", fake_list)
    response = client.post("/api/prompt-templates", json={})
    assert response.status_code == 200 and response.json() == {"templates": [{"api_name": "A"}, {"api_name": "B"}]}


def test_a_plan_with_a_prompt_template_deploys_at_api_64_or_later():
    from types import SimpleNamespace
    from flowtool.planner import PlanStep, StepResult
    t = parse_prompt_template(template_xml(schema=SCHEMA), "Chat_Audit")
    step = StepResult(step=PlanStep(artifact_type="prompt_template", name="T", brief="x"), value=t, repairs=0, messages=[])
    assert server._deploy_api_version(SimpleNamespace(steps=[step], api_version="62.0")) == "64.0"
    assert server._deploy_api_version(SimpleNamespace(steps=[step], api_version="68.0")) == "68.0"
