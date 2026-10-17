"""
An AI review of every finished conversation, end to end through the real
planner machinery: plan -> execute_plan -> the deployable bundle.

The model's own answers are scripted (a quota-free stand-in for a provider), so
this proves what is ours: step ordering and dependency facts, every generator's
validation, and the metadata the bundle compiles to. It is run twice with
different names to show nothing is tied to one object - QA_Audit_Review__c is
only an example.
"""

import io
import json
import re
import zipfile

import pytest

import server
from flowtool.ir import Flow
from flowtool.ir_prompt_template import PromptTemplate
from flowtool.planner import Plan, execute_plan
from flowtool.sfdc import build_deploy_package
from tests.test_llm import TypedScriptedProvider

SCENARIOS = {
    "qa": dict(
        obj="QA_Audit_Review__c", label="QA Audit Review", prefix="QA", score="Overall_Score__c",
        breach="Compliance_Breach__c", sentiment="Sentiment__c", reason="Breach_Reason__c",
        summary="Executive_Summary__c", agent="Agent__c", session="Messaging_Session__c",
        review="Needs_Manager_Review__c", template="QA_Audit_Scoring", flow="QA_Audit_On_Session_End",
        builder="QAAuditTranscriptBuilder", writer="QAAuditWriter",
    ),
    "calls": dict(
        obj="Call_Review__c", label="Call Review", prefix="CR", score="Quality_Score__c",
        breach="Policy_Violation__c", sentiment="Mood__c", reason="Violation_Details__c",
        summary="Short_Summary__c", agent="Rep__c", session="Chat_Session__c",
        review="Escalate__c", template="Call_Grader", flow="Grade_Ended_Chats",
        builder="ChatTextBuilder", writer="CallReviewWriter",
    ),
}


def field(n, api, label, type, **kw):
    return {"api_name": api, "label": label, "type": type, "object_api_name": n["obj"], **kw}


def payloads(n):
    obj = {"api_name": n["obj"], "label": n["label"], "plural_label": n["label"] + "s",
           "record_name_type": "AutoNumber", "record_name_display_format": n["prefix"] + "-{00000}"}
    fields = [
        field(n, n["score"], "Score", "Number", precision=3, scale=0),
        field(n, n["breach"], "Breach", "Checkbox"),
        field(n, n["sentiment"], "Sentiment", "Picklist", picklist_values=["Positive", "Neutral", "Negative"]),
        field(n, n["reason"], "Reason", "LongTextArea"),
        field(n, n["summary"], "Summary", "TextArea"),
        field(n, n["agent"], "Agent", "Lookup", reference_to="User", relationship_name=n["agent"][:-3] + "_Reviews"),
        field(n, n["session"], "Session", "Lookup", reference_to="MessagingSession",
              relationship_name=n["session"][:-3] + "_Reviews"),
    ]
    formula = field(n, n["review"], "Needs Review", "Formula", formula_returns="Checkbox",
                    formula=f'OR({n["score"]} < 60, {n["breach"]}, ISPICKVAL({n["sentiment"]}, "Negative"))')
    template = {
        "api_name": n["template"], "label": n["template"].replace("_", " "),
        "content": "Review this customer conversation. Return the score, whether a policy was breached and why, "
                   "the customer's mood and a one-paragraph summary.\n\nConversation:\n{!$Input:transcript}",
        "inputs": [{"name": "transcript", "type": "String"}],
        "response_format": "JSON",
        "output_schema": {
            "type": "object",
            "properties": {
                "overall_score": {"type": "integer"}, "compliance_breach": {"type": "boolean"},
                "breach_reason": {"type": "string"},
                "customer_sentiment": {"type": "string", "enum": ["Positive", "Neutral", "Negative"]},
                "executive_summary": {"type": "string"},
            },
            "required": ["overall_score", "compliance_breach", "customer_sentiment"],
        },
    }
    builder = {"api_name": n["builder"], "body": f"""public with sharing class {n["builder"]} {{
    public class Request {{
        @InvocableVariable(required=true) public Id sessionId;
    }}
    public class Result {{
        @InvocableVariable public String transcript;
    }}
    @InvocableMethod(label='Build transcript' description='Joins a messaging session conversation into text')
    public static List<Result> build(List<Request> requests) {{
        Set<Id> sessionIds = new Set<Id>();
        for (Request r : requests) {{ sessionIds.add(r.sessionId); }}
        Map<Id, Id> conversationBySession = new Map<Id, Id>();
        for (MessagingSession s : [SELECT Id, ConversationId FROM MessagingSession WHERE Id IN :sessionIds]) {{
            conversationBySession.put(s.Id, s.ConversationId);
        }}
        Map<Id, List<String>> lines = new Map<Id, List<String>>();
        for (ConversationEntry e : [SELECT ConversationId, ActorType, Message FROM ConversationEntry
                                    WHERE ConversationId IN :conversationBySession.values() ORDER BY ConversationId, Seq]) {{
            if (String.isBlank(e.Message)) {{ continue; }}
            if (!lines.containsKey(e.ConversationId)) {{ lines.put(e.ConversationId, new List<String>()); }}
            lines.get(e.ConversationId).add(e.ActorType + ': ' + e.Message);
        }}
        List<Result> out = new List<Result>();
        for (Request r : requests) {{
            Result res = new Result();
            List<String> own = lines.get(conversationBySession.get(r.sessionId));
            res.transcript = own == null ? '' : String.join(own, '\\n');
            out.add(res);
        }}
        return out;
    }}
}}
"""}
    writer = {"api_name": n["writer"], "body": f"""public with sharing class {n["writer"]} {{
    public class Request {{
        @InvocableVariable(required=true) public Id sessionId;
        @InvocableVariable public String agentId;
        @InvocableVariable(required=true) public String answerJson;
    }}
    public class Answer {{
        public Integer overall_score;
        public Boolean compliance_breach;
        public String breach_reason;
        public String customer_sentiment;
        public String executive_summary;
    }}
    @InvocableMethod(label='Record review' description='Saves the model answer as a review record')
    public static void record(List<Request> requests) {{
        List<{n["obj"]}> rows = new List<{n["obj"]}>();
        for (Request r : requests) {{
            Answer a = new Answer();
            try {{
                a = (Answer) JSON.deserialize(r.answerJson, Answer.class);
            }} catch (Exception e) {{
                a = new Answer();
            }}
            Integer score = a.overall_score == null ? null : Math.max(0, Math.min(100, a.overall_score));
            {n["obj"]} row = new {n["obj"]}();
            row.{n["session"]} = r.sessionId;
            row.{n["score"]} = score;
            row.{n["breach"]} = a.compliance_breach == true;
            row.{n["sentiment"]} = a.customer_sentiment;
            row.{n["reason"]} = a.breach_reason;
            row.{n["summary"]} = a.executive_summary == null ? null : a.executive_summary.left(255);
            if (r.agentId != null && r.agentId.startsWith('005')) {{ row.{n["agent"]} = r.agentId; }}
            rows.add(row);
        }}
        insert rows;
    }}
}}
"""}
    flow = {
        "api_name": n["flow"], "label": n["flow"].replace("_", " "), "api_version": "64.0",
        "start": {
            "object": "MessagingSession", "trigger_type": "RecordAfterSave", "record_trigger_type": "Update",
            "filters": [{"field": "Status", "operator": "EqualTo", "value": {"string_value": "Ended"}}],
            "only_when_changed_to_meet_criteria": True,
            "scheduled_paths": [{"name": "Review_Async", "run_asynchronously": True, "next": "Build_Transcript"}],
        },
        "elements": [
            {"type": "ActionCall", "name": "Build_Transcript", "label": "Build transcript",
             "action_name": n["builder"], "action_type": "apex", "store_output_automatically": True,
             "input_parameters": [{"name": "sessionId", "value": {"element_reference": "$Record.Id"}}],
             "next": "Score_Conversation"},
            {"type": "ActionCall", "name": "Score_Conversation", "label": "Score conversation",
             "action_name": n["template"], "action_type": "generatePromptResponse", "store_output_automatically": True,
             "input_parameters": [{"name": "Input:transcript", "value": {"element_reference": "Build_Transcript.transcript"}}],
             "next": "Save_Review"},
            {"type": "ActionCall", "name": "Save_Review", "label": "Save review",
             "action_name": n["writer"], "action_type": "apex",
             "input_parameters": [
                 {"name": "sessionId", "value": {"element_reference": "$Record.Id"}},
                 {"name": "agentId", "value": {"element_reference": "$Record.OwnerId"}},
                 {"name": "answerJson", "value": {"element_reference": "Score_Conversation.promptResponse"}}]},
        ],
    }
    plan = {"reasoning": "An object for the result, a JSON prompt, two Apex helpers and an async flow.", "steps": [
        {"artifact_type": "object", "name": "Review object", "brief": f"{n['obj']} to hold a review"},
        *[{"artifact_type": "field", "name": f"F{i}", "brief": f"field {i}", "depends_on": ["Review object"]}
          for i in range(len(fields))],
        {"artifact_type": "field", "name": "Formula", "brief": "needs-review formula",
         "depends_on": [f"F{i}" for i in (0, 1, 2)]},
        {"artifact_type": "prompt_template", "name": "Scoring prompt", "brief": "score a conversation"},
        {"artifact_type": "apex", "name": "Transcript builder", "brief": "build a transcript"},
        {"artifact_type": "apex", "name": "Review writer", "brief": "parse the JSON and save the record",
         "depends_on": ["Scoring prompt", "Review object", "Formula"]},
        {"artifact_type": "flow", "name": "Review flow", "brief": "run it when a session ends",
         "depends_on": ["Scoring prompt", "Transcript builder", "Review writer"]},
    ]}
    return plan, dict(object=obj, field=fields + [formula], prompt_template=template, apex=[builder, writer], flow=flow)


@pytest.fixture(params=sorted(SCENARIOS))
def run(request):
    n = SCENARIOS[request.param]
    plan_payload, typed = payloads(n)
    provider = TypedScriptedProvider(plan=plan_payload, **typed)
    results = execute_plan(provider, Plan.model_validate(plan_payload))
    return n, provider, results


def test_every_step_generates_and_validates(run):
    n, _, results = run
    assert len(results) == 13
    kinds = sorted(r.step.artifact_type for r in results)
    assert kinds == ["apex", "apex", "field", "field", "field", "field", "field", "field", "field", "field",
                     "flow", "object", "prompt_template"]


def test_the_flow_is_told_the_real_template_and_helpers(run):
    n, provider, _ = run
    brief = [c for c in provider.calls if c[0] == "flow"][0][1][0].content
    assert "generatePromptResponse" in brief and n["template"] in brief
    assert "Input:transcript" in brief and "overall_score" in brief and "promptResponse" in brief
    assert n["builder"] in brief and n["writer"] in brief


def test_the_writer_is_told_the_json_keys_to_parse(run):
    n, provider, _ = run
    writer_calls = [c for c in provider.calls if c[0] == "apex"]
    briefs = [c[1][0].content for c in writer_calls]
    assert any("customer_sentiment" in b and n["template"] in b for b in briefs)


def test_the_bundle_has_every_component_type(run):
    n, _, results = run
    files, types = server._bundle_files_and_types(results)
    assert set(types) == {"CustomObject", "GenAiPromptTemplate", "ApexClass", "Flow"}
    assert f"objects/{n['obj']}.object" in files
    assert f"genAiPromptTemplates/{n['template']}.genAiPromptTemplate" in files
    assert f"flows/GC_{n['flow']}.flow" in files
    assert sorted(types["ApexClass"]) == sorted([n["builder"], n["writer"]])


def test_the_object_file_holds_every_field_including_the_formula(run):
    n, _, results = run
    files, _ = server._bundle_files_and_types(results)
    xml = files[f"objects/{n['obj']}.object"]
    for key in ("score", "breach", "sentiment", "reason", "summary", "agent", "session", "review"):
        assert f"<fullName>{n[key]}</fullName>" in xml, key
    assert "<type>LongTextArea</type>" in xml and "<type>TextArea</type>" in xml
    assert "<formula>OR(" in xml


def test_the_flow_runs_only_asynchronously_and_chains_the_three_actions(run):
    n, _, results = run
    files, _ = server._bundle_files_and_types(results)
    xml = files[f"flows/GC_{n['flow']}.flow"]
    assert "<pathType>AsyncAfterCommit</pathType>" in xml
    order = [xml.index(f"<actionName>{a}</actionName>") for a in (n["builder"], n["template"], n["writer"])]
    assert order == sorted(order)
    assert "<actionType>generatePromptResponse</actionType>" in xml


def test_no_scenario_leaks_another_scenarios_names(run):
    n, _, results = run
    files, _ = server._bundle_files_and_types(results)
    other = SCENARIOS["calls" if n is SCENARIOS["qa"] else "qa"]
    blob = "\n".join(files.values())
    assert other["obj"] not in blob and other["flow"] not in blob


def test_the_package_zip_builds(run):
    _, _, results = run
    files, types = server._bundle_files_and_types(results)
    names = zipfile.ZipFile(io.BytesIO(build_deploy_package(files, types, "64.0"))).namelist()
    assert "package.xml" in names and len(names) == len(files) + 1
