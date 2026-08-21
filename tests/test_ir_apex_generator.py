"""
ApexClassGenerator reuses llm.py's generic repair loop, but its real gate is
`_extra_error` - the heuristic check - since there is no Pydantic structure to
lean on for the body. These pin down that a heuristic failure goes round the
loop exactly like a validation error does for Flow.
"""

from flowtool.ir_apex import ApexClass
from flowtool.llm import ApexClassGenerator
from tests.test_llm import ScriptedProvider

VALID = {
    "api_name": "Invoice_Helper",
    "body": "public class Invoice_Helper {\n    public static void run() {}\n}",
}

UNBALANCED = {
    "api_name": "Invoice_Helper",
    "body": "public class Invoice_Helper {\n    public static void run() {}\n",
}

NAME_MISMATCH = {
    "api_name": "Invoice_Helper",
    "body": "public class Wrong_Name {}",
}


class TestApexClassGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID)
        result = ApexClassGenerator(provider).generate("a helper class for invoices")
        assert result.repairs == 0
        assert isinstance(result.value, ApexClass)
        assert result.value.api_name == "Invoice_Helper"

    def test_unbalanced_braces_are_repaired(self):
        provider = ScriptedProvider(UNBALANCED, VALID)
        result = ApexClassGenerator(provider).generate("...")
        assert result.repairs == 1

        complaint = provider.calls[1][-1].content
        assert "unbalanced brackets" in complaint

    def test_name_mismatch_is_repaired(self):
        provider = ScriptedProvider(NAME_MISMATCH, VALID)
        result = ApexClassGenerator(provider).generate("...")
        assert result.repairs == 1

        complaint = provider.calls[1][-1].content
        assert "declared as 'Wrong_Name'" in complaint
        assert "Invoice_Helper" in complaint

    def test_heuristic_check_runs_before_pydantic_validation(self):
        # An unbalanced body is also a perfectly valid non-empty string, so if
        # the heuristic didn't run first this would sail through
        # model_validate and be accepted outright.
        provider = ScriptedProvider(UNBALANCED, VALID)
        ApexClassGenerator(provider).generate("...")
        assert len(provider.calls) == 2, "should have needed a second attempt"
