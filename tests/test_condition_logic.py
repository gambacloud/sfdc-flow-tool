"""
Custom condition logic: "1 OR (2 AND 3)" instead of plain and/or.

This is the second free-form string in the IR, after a formula expression, and
it is the more dangerous of the two. A formula at least looks like something
that needs checking. A logic expression looks structured, reads as though
something must be validating it, and nothing is: Salesforce accepts a number
past the end of the condition list, an unclosed bracket, and the literal string
"banana". All three pass checkOnly and deploy. Verified against a real org.

So every assertion here is the only thing standing between a wrong expression
and a flow that quietly takes the wrong branch.
"""

import re

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Condition,
    Decision,
    Flow,
    GetRecords,
    Outcome,
    RecordFilter,
    Start,
    Value,
    referenced_conditions,
)
from flowtool.mermaid import to_markdown
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate


def condition(n: int) -> Condition:
    return Condition(left="varN", operator="GreaterThan", right=Value(number_value=n))


def outcome(logic: str, how_many: int = 3) -> Outcome:
    return Outcome(
        name="Yes", label="Yes",
        conditions=[condition(n) for n in range(1, how_many + 1)],
        condition_logic=logic,
    )


def flow_with(logic: str, how_many: int = 3) -> Flow:
    return Flow(
        api_name="Check_Flow", label="Check Flow",
        start=Start(next="Check"),
        elements=[Decision(name="Check", label="Check",
                           outcomes=[outcome(logic, how_many)])],
    )


class TestTheExpressionParser:
    @pytest.mark.parametrize("expression,expected", [
        ("1", {1}),
        ("1 AND 2", {1, 2}),
        ("1 OR (2 AND 3)", {1, 2, 3}),
        ("(1 OR 2) AND (3 OR 4)", {1, 2, 3, 4}),
        ("NOT 1", {1}),
        ("1 AND NOT 2", {1, 2}),
        ("((1))", {1}),
        ("1 AND 2 OR 3", {1, 2, 3}),
        # Flow Builder writes them uppercase; the org accepts any case, and an
        # imported flow has to open either way.
        ("1 or 2", {1, 2}),
        ("1 And 2", {1, 2}),
        # Whitespace is not meaningful.
        ("1OR2", {1, 2}),
        ("  1   OR   2  ", {1, 2}),
        # The same condition twice is odd but unambiguous.
        ("1 OR 1", {1}),
    ])
    def test_valid_expressions_report_what_they_use(self, expression, expected):
        assert referenced_conditions(expression) == expected

    @pytest.mark.parametrize("expression,because", [
        ("", "empty"),
        ("   ", "empty"),
        ("banana", "not a condition number"),
        ("1 XOR 2", "left over"),
        ("1 2", "left over"),
        ("1 AND", "stops where a condition number was expected"),
        ("AND 1", "not a condition number"),
        ("1 OR (2 AND 3", "never closed"),
        ("1 OR 2) AND 3", re.escape("no '(' to match it")),
        ("()", "not a condition number"),
    ])
    def test_malformed_expressions_say_what_is_wrong(self, expression, because):
        with pytest.raises(ValueError, match=because):
            referenced_conditions(expression)


class TestTheIrChecksTheNumbers:
    """
    The org checks none of this. Everything below deploys cleanly today.
    """

    def test_a_number_past_the_end_is_refused(self):
        with pytest.raises(ValidationError, match="only 3"):
            outcome("1 AND 4")

    def test_zero_is_refused(self):
        """Conditions are numbered from 1, so 0 names nothing."""
        with pytest.raises(ValidationError, match="condition 0"):
            outcome("0 OR 1")

    def test_the_message_says_where_the_numbering_starts(self):
        with pytest.raises(ValidationError, match="numbered from 1"):
            outcome("1 AND 4")

    def test_a_malformed_expression_is_refused(self):
        with pytest.raises(ValidationError, match="cannot be read"):
            outcome("1 OR (2 AND 3")

    def test_plain_and_or_still_work(self):
        assert outcome("and").condition_logic == "and"
        assert outcome("or").condition_logic == "or"

    def test_a_condition_the_expression_ignores_is_allowed(self):
        """
        Odd, not broken: it is evaluated and discarded. Refusing it would be
        guessing at intent rather than following a rule, so the approval
        document points it out and a person decides.
        """
        assert outcome("1 AND 2").condition_logic == "1 AND 2"

    def test_the_single_condition_case(self):
        assert outcome("1", how_many=1).condition_logic == "1"
        with pytest.raises(ValidationError, match="only 1"):
            outcome("1 OR 2", how_many=1)


class TestFilterLogic:
    """The same string, on the four elements that filter records."""

    def test_get_records_checks_its_filters(self):
        with pytest.raises(ValidationError, match="only 2"):
            GetRecords(
                name="Find", label="Find", object="Account",
                filters=[
                    RecordFilter(field="Rating", operator="EqualTo",
                                 value=Value(string_value="Hot")),
                    RecordFilter(field="Industry", operator="EqualTo",
                                 value=Value(string_value="Banking")),
                ],
                filter_logic="1 OR (2 AND 3)",
            )

    def test_a_valid_filter_expression_is_kept(self):
        element = GetRecords(
            name="Find", label="Find", object="Account",
            filters=[
                RecordFilter(field="Rating", operator="EqualTo",
                             value=Value(string_value="Hot")),
                RecordFilter(field="Industry", operator="EqualTo",
                             value=Value(string_value="Banking")),
            ],
            filter_logic="1 OR 2",
        )
        assert element.filter_logic == "1 OR 2"

    def test_the_start_element_checks_its_entry_conditions(self):
        with pytest.raises(ValidationError, match="entry conditions"):
            Start(
                object="Account", record_trigger_type="Update",
                trigger_type="RecordAfterSave",
                filters=[RecordFilter(field="Rating", operator="EqualTo",
                                      value=Value(string_value="Hot"))],
                filter_logic="1 AND 2",
            )

    def test_no_filters_means_no_expression(self):
        with pytest.raises(ValidationError, match="only 0"):
            GetRecords(name="Find", label="Find", object="Account",
                       filter_logic="1")


class TestRoundTrip:
    def test_a_custom_expression_survives(self):
        before = flow_with("1 OR (2 AND 3)")
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.model_dump() == before.model_dump()

    def test_the_expression_is_written_back_exactly(self):
        """
        Never reformatted. An expression that comes back spelled differently is
        a diff against the org version for no reason, and the one thing a
        reviewer compares by eye.
        """
        for expression in ["1 OR (2 AND 3)", "1 or 2", "NOT 1", "  1 AND 2  "]:
            flow = flow_with(expression)
            after = parse_flow(generate(flow), api_name=flow.api_name)
            assert after.elements[0].outcomes[0].condition_logic == expression

    def test_and_is_still_the_default(self):
        flow = parse_flow(generate(flow_with("and")), api_name="Check_Flow")
        assert flow.elements[0].outcomes[0].condition_logic == "and"


class TestWhatTheUserSees:
    def test_the_expression_is_shown_as_itself(self):
        """
        Joining the conditions with the expression the way `and` is joined
        would read as though every pair were combined that way.
        """
        markdown = to_markdown(flow_with("1 OR (2 AND 3)"), include_diagram=False)
        assert "1 OR (2 AND 3), where" in markdown

    def test_the_conditions_are_numbered_to_match(self):
        markdown = to_markdown(flow_with("1 OR (2 AND 3)"), include_diagram=False)
        for n in (1, 2, 3):
            assert f"({n}) varN >" in markdown

    def test_a_condition_the_expression_ignores_is_called_out(self):
        """
        The IR allows it, so this line is the only warning anyone gets.
        """
        markdown = to_markdown(flow_with("1 AND 2"), include_diagram=False)
        assert "nothing in the expression uses 3" in markdown

    def test_plain_logic_reads_as_a_sentence(self):
        markdown = to_markdown(flow_with("and"), include_diagram=False)
        assert " AND " in markdown
        assert "where (1)" not in markdown
