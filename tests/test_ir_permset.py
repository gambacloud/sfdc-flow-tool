from dataclasses import dataclass
from typing import Any

from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomField, CustomObject
from flowtool.ir_permset import READ_ONLY_FIELD_TYPES, build_grant_from_steps, sanitize_api_name


@dataclass
class _FakeStepResult:
    """A minimal stand-in for planner.StepResult - build_grant_from_steps
    only reads `.value`, so a real StepResult (with its Plan/PlanStep
    machinery) isn't needed here."""

    value: Any


def _field(**kwargs):
    defaults = dict(api_name="Foo__c", label="Foo", type="Text", object_api_name="Account")
    defaults.update(kwargs)
    return CustomField(**defaults)


def _object(**kwargs):
    defaults = dict(api_name="Invoice__c", label="Invoice", plural_label="Invoices")
    defaults.update(kwargs)
    return CustomObject(**defaults)


_TYPE_SPECIFIC_KWARGS = {
    "Text": {},
    "Number": {"precision": 10, "scale": 2},
    "Checkbox": {},
    "Picklist": {"picklist_values": ["A", "B"]},
    "Lookup": {"reference_to": "Account"},
    "MasterDetail": {"reference_to": "Account"},
}


class TestBuildGrantFromSteps:
    def test_every_current_field_type_is_editable(self):
        steps = [
            _FakeStepResult(_field(type=t, api_name=f"{t}__c", **_TYPE_SPECIFIC_KWARGS[t]))
            for t in ("Text", "Number", "Checkbox", "Picklist", "Lookup", "MasterDetail")
        ]
        grant = build_grant_from_steps(steps, "Test Grant")
        assert len(grant.field_grants) == 6
        assert all(fg.editable for fg in grant.field_grants)
        assert all(fg.readable for fg in grant.field_grants)

    def test_field_member_names_carry_object_api_name(self):
        steps = [_FakeStepResult(_field(api_name="Amount__c", object_api_name="Invoice__c"))]
        grant = build_grant_from_steps(steps, "Test Grant")
        fg = grant.field_grants[0]
        assert fg.field_api_name == "Amount__c"
        assert fg.object_api_name == "Invoice__c"

    def test_object_gets_full_crud_minus_delete(self):
        steps = [_FakeStepResult(_object(api_name="Invoice__c"))]
        grant = build_grant_from_steps(steps, "Test Grant")
        assert len(grant.object_grants) == 1
        og = grant.object_grants[0]
        assert og.object_api_name == "Invoice__c"
        assert og.allow_read and og.allow_create and og.allow_edit
        assert not og.allow_delete

    def test_non_field_non_object_steps_are_ignored(self):
        steps = [
            _FakeStepResult(ApexClass(api_name="Foo", body="public class Foo {}")),
            _FakeStepResult(_field()),
        ]
        grant = build_grant_from_steps(steps, "Test Grant")
        assert len(grant.field_grants) == 1
        assert len(grant.object_grants) == 0

    def test_read_only_field_types_is_empty_today(self):
        # ir_object.py's FieldType deliberately has no calculated/formula
        # type yet - this pins down that build_grant_from_steps' "editable
        # unless read-only" logic has nothing to actually exclude today,
        # without hardcoding that fact into the assertions above.
        assert READ_ONLY_FIELD_TYPES == frozenset()

    def test_mixed_plan_produces_both_kinds_of_grant(self):
        steps = [
            _FakeStepResult(_object(api_name="Invoice__c")),
            _FakeStepResult(_field(api_name="Amount__c", object_api_name="Invoice__c")),
        ]
        grant = build_grant_from_steps(steps, "Invoice Access")
        assert grant.label == "Invoice Access"
        assert len(grant.object_grants) == 1
        assert len(grant.field_grants) == 1


class TestSanitizeApiName:
    def test_already_valid_name_is_unchanged(self):
        assert sanitize_api_name("Invoice_Access") == "Invoice_Access"

    def test_spaces_become_underscores(self):
        assert sanitize_api_name("CS Onboarding Access") == "CS_Onboarding_Access"

    def test_punctuation_collapses_to_a_single_underscore(self):
        assert sanitize_api_name("Support's Access - Tier 1!") == "Support_s_Access_Tier_1"

    def test_leading_digit_gets_a_letter_prefix(self):
        assert sanitize_api_name("2026 Access") == "X_2026_Access"

    def test_blank_label_falls_back_to_a_default(self):
        assert sanitize_api_name("   ") == "Generated_Access"

    def test_result_is_always_a_valid_api_name(self):
        import re

        from flowtool.ir import API_NAME_RE

        for label in ("CS Onboarding Access", "2026 Access", "!!!", "a" * 200, "Already_Valid"):
            assert API_NAME_RE.match(sanitize_api_name(label)), label

    def test_result_never_exceeds_the_permission_set_name_limit(self):
        assert len(sanitize_api_name("A " * 100)) <= 80
