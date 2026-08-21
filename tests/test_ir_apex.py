"""
The Apex IR's job is thin: reject an empty body and a bad api_name, the same
as every other IR. heuristic_errors is the real gate - these pin down what it
catches and what it deliberately doesn't (see ir_apex.py's module docstring).
"""

import pytest
from pydantic import ValidationError

from flowtool.ir_apex import ApexClass, heuristic_errors

VALID_BODY = "public class Invoice_Helper {\n    public static void run() {}\n}"


class TestApexClassModel:
    def test_valid_class_is_accepted(self):
        cls = ApexClass(api_name="Invoice_Helper", body=VALID_BODY)
        assert cls.status == "Active"
        assert cls.api_version == "62.0"

    def test_empty_body_is_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            ApexClass(api_name="Invoice_Helper", body="   ")

    @pytest.mark.parametrize("name", ["1_First", "trailing_", "double__under"])
    def test_rejects_invalid_api_name(self, name):
        with pytest.raises(ValidationError, match="not a valid Salesforce API name"):
            ApexClass(api_name=name, body=VALID_BODY)

    def test_class_name_over_40_chars_is_rejected(self):
        # Salesforce's real limit on an Apex class name.
        too_long = "A" * 41
        with pytest.raises(ValidationError, match="over Salesforce's 40-character limit"):
            ApexClass(api_name=too_long, body=f"public class {too_long} {{}}")

    def test_class_name_at_exactly_40_chars_is_accepted(self):
        exactly = "A" * 40
        cls = ApexClass(api_name=exactly, body=f"public class {exactly} {{}}")
        assert cls.api_name == exactly


class TestHeuristicErrors:
    def test_valid_class_has_no_problems(self):
        assert heuristic_errors("Invoice_Helper", VALID_BODY) == []

    def test_unclosed_brace_is_caught(self):
        problems = heuristic_errors("Foo", "public class Foo {\n    void run() {}\n")
        assert any("unbalanced brackets" in p for p in problems)

    def test_stray_closing_brace_is_caught(self):
        problems = heuristic_errors("Foo", "public class Foo { void run() {} } }")
        assert any("unbalanced" in p for p in problems)

    def test_mismatched_bracket_kind_is_caught(self):
        # A '(' closed by ']' - the kind of thing a balance-only check would
        # miss if it only counted opens vs. closes instead of tracking a stack.
        problems = heuristic_errors("Foo", "public class Foo { void run(] {} }")
        assert any("unbalanced" in p for p in problems)

    def test_missing_class_declaration_is_caught(self):
        problems = heuristic_errors("Foo", "public void run() {}")
        assert any("no 'class" in p for p in problems)

    def test_name_mismatch_is_caught(self):
        problems = heuristic_errors("Invoice_Helper", "public class Wrong_Name {}")
        assert any("declared as 'Wrong_Name'" in p for p in problems)
        assert any("Invoice_Helper" in p for p in problems)

    def test_inner_class_does_not_confuse_the_declaration_match(self):
        # The outer class is the one that must match api_name; an inner class
        # sharing the file is legitimate Apex and must not be flagged.
        body = "public class Outer {\n    class Inner {}\n}"
        problems = heuristic_errors("Outer", body)
        assert problems == []
