"""PromptTemplate IR + XML, and the Flow IR change that lets a flow call one."""

import json
import xml.etree.ElementTree as ET

import pytest

from flowtool.ir import Flow
from flowtool.ir_prompt_template import PromptInput, PromptTemplate
from flowtool.xmlgen import METADATA_NS, generate
from flowtool.xmlgen_prompt_template import generate_prompt_template

SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}, "breach": {"type": "boolean"}},
          "required": ["score"]}


def template(**kw):
    base = dict(api_name="QA_Audit_Scoring", label="QA Audit Scoring",
                content="Score this: {!$Input:transcript}", inputs=[PromptInput(name="transcript")])
    base.update(kw)
    return PromptTemplate(**base)


def test_content_must_reference_declared_inputs_only():
    with pytest.raises(ValueError, match="not declared"):
        template(content="Hello {!$Input:nobody}")


def test_required_input_must_be_used():
    with pytest.raises(ValueError, match="never used"):
        template(content="No variables here")


def test_optional_input_may_go_unused():
    template(content="No variables here", inputs=[PromptInput(name="x", required=False)])


def test_duplicate_inputs_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        template(inputs=[PromptInput(name="transcript"), PromptInput(name="transcript")])


def test_json_needs_a_schema_and_text_must_not_have_one():
    with pytest.raises(ValueError, match="needs output_schema"):
        template(response_format="JSON")
    with pytest.raises(ValueError, match="only applies"):
        template(output_schema=SCHEMA)


def test_schema_required_keys_must_exist():
    bad = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]}
    with pytest.raises(ValueError, match="undeclared"):
        template(response_format="JSON", output_schema=bad)


def test_xml_for_a_json_template():
    t = template(response_format="JSON", output_schema=SCHEMA)
    root = ET.fromstring(generate_prompt_template(t))
    ns = {"m": METADATA_NS}
    v = root.find("m:templateVersions", ns)
    assert root.findtext("m:type", namespaces=ns) == "einstein_gpt__flex"
    assert v.findtext("m:responseFormat", namespaces=ns) == "JSON"
    assert json.loads(v.findtext("m:outputSchema", namespaces=ns)) == SCHEMA
    inp = v.find("m:inputs", ns)
    assert inp.findtext("m:referenceName", namespaces=ns) == "Input:transcript"
    assert inp.findtext("m:definition", namespaces=ns) == "primitive://String"
    assert v.find("m:versionIdentifier", ns) is None  # the org rejects an invented one


def test_text_template_has_no_schema_elements():
    xml = generate_prompt_template(template())
    assert "outputSchema" not in xml and "responseFormat" not in xml


def test_output_keys():
    assert template(response_format="JSON", output_schema=SCHEMA).output_keys() == ["score", "breach"]


def test_content_is_xml_escaped():
    xml = generate_prompt_template(template(content="a < b & {!$Input:transcript}"))
    assert "a &lt; b &amp;" in xml


# --- a flow can call it -------------------------------------------------------

def async_only_flow():
    return Flow.model_validate({
        "api_name": "QA_Audit_On_Session_End", "label": "QA Audit", "api_version": "64.0",
        "start": {
            "object": "MessagingSession", "trigger_type": "RecordAfterSave", "record_trigger_type": "Update",
            "filters": [{"field": "Status", "operator": "EqualTo", "value": {"string_value": "Ended"}}],
            "only_when_changed_to_meet_criteria": True,
            "scheduled_paths": [{"name": "Audit_Async", "run_asynchronously": True, "next": "Call_Prompt"}],
        },
        "variables": [{"name": "transcript", "data_type": "String"}],
        "elements": [{
            "type": "ActionCall", "name": "Call_Prompt", "label": "Score", "action_name": "QA_Audit_Scoring",
            "action_type": "generatePromptResponse", "store_output_automatically": True,
            "input_parameters": [{"name": "Input:transcript", "value": {"element_reference": "transcript"}}],
        }],
    })


def test_flow_with_only_an_async_path_is_valid_and_calls_the_prompt():
    xml = generate(async_only_flow())
    assert "<actionType>generatePromptResponse</actionType>" in xml
    assert "<actionName>QA_Audit_Scoring</actionName>" in xml
    assert "<name>Input:transcript</name>" in xml


def test_flow_with_nothing_connected_is_still_rejected():
    data = async_only_flow().model_dump()
    data["start"]["scheduled_paths"][0]["next"] = None
    with pytest.raises(ValueError, match="nothing is connected"):
        Flow.model_validate(data)


# --- the call shape the org accepts -------------------------------------------

def test_calling_the_action_type_by_its_own_name_is_refused():
    data = async_only_flow().model_dump()
    data["elements"][0]["action_name"] = "generatePromptResponse"
    with pytest.raises(ValueError, match="template's own api_name"):
        Flow.model_validate(data)


def test_a_prompt_template_input_parameter_is_refused():
    data = async_only_flow().model_dump()
    data["elements"][0]["input_parameters"].append(
        {"name": "promptTemplate", "value": {"string_value": "QA_Audit_Scoring"}}
    )
    with pytest.raises(ValueError, match="remove the 'promptTemplate' input"):
        Flow.model_validate(data)
