"""
Four small things a 91-flow corpus found that a 15-flow one could not.

None of them is a feature. Each is a place where the IR was narrower than
Salesforce, and every one of them refused a flow that is live in an org right
now. Together they took the public corpus from 65 parsed to 72.

Three were settled by asking the org which spellings and operators are real
rather than copying what the corpus happened to contain. That mattered: the org
turned out to *enforce* these enums - "'Banana' is not a valid value for the
enum 'FlowAssignmentOperator'" - which is the opposite of condition logic, where
it checks nothing. Widening an enum the org polices is safe in a way that
widening a free-form string is not.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Assignment,
    AssignmentItem,
    Flow,
    GetRecords,
    Start,
    Value,
)
from flowtool.mermaid import to_markdown
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate


def org_xml(body: str, status: str = "Draft") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
        "<apiVersion>62.0</apiVersion><label>X</label>"
        f"<processType>AutoLaunchedFlow</processType><status>{status}</status>"
        f"{body}"
        "<start><connector><targetReference>Set_It</targetReference></connector>"
        "</start></Flow>"
    )


ASSIGNMENT = (
    "<assignments><name>Set_It</name><label>Set It</label>"
    "<assignmentItems><assignToReference>text</assignToReference>"
    "<operator>{operator}</operator><value>{value}</value>"
    "</assignmentItems></assignments>"
)


def assignment_xml(operator="Assign", value="<stringValue>x</stringValue>",
                   status="Draft"):
    return org_xml(ASSIGNMENT.format(operator=operator, value=value), status)


class TestStatusSpelling:
    """
    Three live flows in Salesforce's own sample apps say ACTIVE, not Active.
    The org reads FlowVersionStatus without regard to case and deploys both
    identically - so matching on case refused a flow over a spelling.
    """

    @pytest.mark.parametrize("written,expected", [
        ("Active", "Active"),
        ("ACTIVE", "Active"),
        ("active", "Active"),
        ("Draft", "Draft"),
        ("DRAFT", "Draft"),
    ])
    def test_case_is_normalised_on_the_way_in(self, written, expected):
        flow = parse_flow(assignment_xml(status=written), api_name="X")
        assert flow.status == expected

    @pytest.mark.parametrize("status", ["Obsolete", "InvalidDraft"])
    def test_the_states_salesforce_marks_old_versions_with(self, status):
        """
        Never written by this tool, but a flow retrieved from an org can be
        either. Refusing them made an old version unreadable rather than
        unwritable, which is not the same thing.
        """
        flow = parse_flow(assignment_xml(status=status), api_name="X")
        assert flow.status == status

    def test_the_canonical_spelling_is_what_goes_back_out(self):
        """
        Nothing to preserve: the org treats the two as the same value, so this
        is a normalisation rather than a rewrite of someone's content.
        """
        flow = parse_flow(assignment_xml(status="ACTIVE"), api_name="X")
        assert "<status>Active</status>" in generate(flow)


class TestAssignmentOperators:
    """
    Four were modelled - Assign, Add, Subtract, AddItem - out of the twelve the
    org accepts. AssignCount alone appears in two live flows.
    """

    @pytest.mark.parametrize("operator", [
        "Assign", "Add", "Subtract", "AssignCount", "AddItem", "AddAtStart",
        "RemoveFirst", "RemoveBeforeFirst", "RemoveAfterFirst",
        "RemovePosition", "RemoveAll", "RemoveUncommon",
    ])
    def test_every_operator_the_org_accepts_round_trips(self, operator):
        flow = parse_flow(assignment_xml(operator=operator), api_name="X")
        assert flow.elements[0].items[0].operator == operator
        again = parse_flow(generate(flow), api_name="X")
        assert again.model_dump() == flow.model_dump()

    def test_an_operator_the_org_rejects_is_still_refused(self):
        """
        The IR is not simply opened up: the org polices this enum, and the IR
        matches it rather than allowing anything.
        """
        with pytest.raises(ValidationError):
            AssignmentItem(to_reference="v", operator="Banana",
                           value=Value(number_value=1))


class TestTheEmptyString:
    """
    <stringValue /> is an empty string, not a missing value.

    Reading it as missing dropped the assignment item, which left the
    assignment with no items, which failed the model - so a live flow that
    blanks a variable was refused, and the reason named the wrong thing.
    """

    def test_an_empty_string_value_is_read(self):
        flow = parse_flow(
            assignment_xml(value="<stringValue />"), api_name="X"
        )
        assert flow.elements[0].items[0].value.string_value == ""

    def test_it_survives_a_round_trip(self):
        before = Flow(
            api_name="X", label="X", start=Start(next="A"),
            elements=[Assignment(name="A", label="A", items=[
                AssignmentItem(to_reference="text",
                               value=Value(string_value=""))])],
        )
        after = parse_flow(generate(before), api_name="X")
        assert after.model_dump() == before.model_dump()

    def test_an_empty_number_is_still_skipped(self):
        """
        Only a string can legitimately be empty. An empty <numberValue /> is
        malformed, and reading it would mean inventing a number.
        """
        with pytest.raises(Exception):
            parse_flow(assignment_xml(value="<numberValue />"), api_name="X")


class TestUnreachableElementsAreAWarning:
    """
    Salesforce deploys a flow with an element nothing reaches. One of its own
    sample apps ships a live Active one: Update_Profile has no connector and
    Assign_Output is never reached. Refusing it made that flow impossible to
    open at all, which is worse than drawing it with a note attached.
    """

    def orphan_flow(self) -> Flow:
        return Flow(
            api_name="X", label="X", start=Start(next="A"),
            elements=[
                Assignment(name="A", label="A", items=[
                    AssignmentItem(to_reference="v",
                                   value=Value(number_value=1))]),
                GetRecords(name="Stranded", label="Stranded", object="Account"),
            ],
        )

    def test_the_flow_is_accepted(self):
        assert self.orphan_flow().warnings()

    def test_a_flow_from_an_org_with_one_now_opens(self):
        xml = org_xml(
            ASSIGNMENT.format(operator="Assign",
                              value="<stringValue>x</stringValue>")
            + "<recordLookups><name>Stranded</name><label>Stranded</label>"
              "<object>Account</object></recordLookups>"
        )
        flow = parse_flow(xml, api_name="X")
        # A set: the XML groups elements by tag, so their order is not the
        # flow's order and the round trip deliberately does not assert it.
        assert {e.name for e in flow.elements} == {"Stranded", "Set_It"}
        assert "Stranded" in "\n".join(flow.warnings())

    def test_the_approval_document_says_so_above_the_diagram(self):
        """
        An element nothing reaches draws like any other and gives no sign that
        it never runs, so the note has to come before the picture.
        """
        markdown = to_markdown(self.orphan_flow(), include_diagram=False)
        assert "[!WARNING]" in markdown
        assert "Stranded" in markdown
        assert markdown.index("[!WARNING]") < markdown.index("## Trigger")

    def test_a_connected_flow_gets_no_warning_section(self):
        flow = Flow(
            api_name="X", label="X", start=Start(next="A"),
            elements=[Assignment(name="A", label="A", items=[
                AssignmentItem(to_reference="v",
                               value=Value(number_value=1))])],
        )
        assert "[!WARNING]" not in to_markdown(flow, include_diagram=False)

    def test_a_start_that_connects_to_nothing_is_still_an_error(self):
        """
        Not the same case. Salesforce rejects this one outright: "The flow
        can't run because nothing is connected to the Start element."
        """
        with pytest.raises(ValidationError, match="nothing is connected"):
            Flow(api_name="X", label="X", start=Start(),
                 elements=[Assignment(name="A", label="A", items=[
                     AssignmentItem(to_reference="v",
                                    value=Value(number_value=1))])])
