"""
PromptTemplateGenerator reuses llm.py's generic repair loop; the IR's own
validators (declared inputs, JSON needs a schema) are the whole gate.
"""

from flowtool.ir_prompt_template import PromptTemplate
from flowtool.llm import PROMPT_TEMPLATE_SYSTEM_PROMPT, PromptTemplateGenerator
from tests.test_llm import ScriptedProvider

VALID_PROMPT = {
    "api_name": "QA_Audit_Scoring",
    "label": "QA Audit Scoring",
    "content": "Score this conversation from 0 to 100 and flag breaches. {!$Input:transcript}",
    "inputs": [{"name": "transcript", "type": "String"}],
    "response_format": "JSON",
    "output_schema": {
        "type": "object",
        "properties": {"overall_score": {"type": "integer"}, "compliance_breach": {"type": "boolean"}},
        "required": ["overall_score", "compliance_breach"],
    },
}

UNDECLARED_INPUT = dict(VALID_PROMPT, inputs=[])
JSON_WITHOUT_SCHEMA = {k: v for k, v in VALID_PROMPT.items() if k != "output_schema"}


class TestPromptTemplateGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        result = PromptTemplateGenerator(ScriptedProvider(VALID_PROMPT)).generate("score a conversation")
        assert result.repairs == 0
        assert isinstance(result.value, PromptTemplate)
        assert result.value.output_keys() == ["overall_score", "compliance_breach"]

    def test_undeclared_input_is_repaired(self):
        provider = ScriptedProvider(UNDECLARED_INPUT, VALID_PROMPT)
        result = PromptTemplateGenerator(provider).generate("...")
        assert result.repairs == 1
        assert "not declared" in provider.calls[1][-1].content

    def test_json_without_schema_is_repaired(self):
        provider = ScriptedProvider(JSON_WITHOUT_SCHEMA, VALID_PROMPT)
        assert PromptTemplateGenerator(provider).generate("...").repairs == 1

    def test_prompt_explains_the_input_reference_syntax(self):
        assert "{!$Input:name}" in PROMPT_TEMPLATE_SYSTEM_PROMPT
