"""
The diagram is what the user approves, so it must not misstate the logic.
"""

import pytest

from flowtool.ir import (
    FieldValue,
    InputAssignment,
    RecordCreate,
    Condition,
    Decision,
    Flow,
    GetRecords,
    Outcome,
    RecordFilter,
    Start,
    Transform,
    TransformValue,
    TransformValueAction,
    Value,
)
from flowtool.mermaid import condition_text, filter_text, to_markdown, to_mermaid


class TestUnaryOperators:
    """
    Salesforce negates a unary operator with a boolean on its right: IsNull with
    false means "is not null". Reading only the operator inverts the meaning.
    """

    def test_is_null_true_reads_as_is_null(self):
        assert condition_text(
            Condition(left="$Record.AccountId", operator="IsNull",
                      right=Value(boolean_value=True))
        ) == "$Record.AccountId is null"

    def test_is_null_false_reads_as_is_not_null(self):
        assert condition_text(
            Condition(left="$Record.AccountId", operator="IsNull",
                      right=Value(boolean_value=False))
        ) == "$Record.AccountId is not null"

    def test_bare_is_null_reads_as_is_null(self):
        assert condition_text(
            Condition(left="$Record.AccountId", operator="IsNull")
        ) == "$Record.AccountId is null"

    def test_the_same_holds_for_record_filters(self):
        assert filter_text(
            RecordFilter(field="AccountId", operator="IsNull",
                         value=Value(boolean_value=False))
        ) == "AccountId is not null"


def _flow_with(condition: Condition) -> Flow:
    return Flow(
        api_name="F", label="F",
        start=Start(next="D"),
        elements=[
            Decision(
                name="D", label="Check",
                outcomes=[Outcome(name="Yes", label="Yes", conditions=[condition],
                                  next="G")],
            ),
            GetRecords(name="G", label="Get", object="Account"),
        ],
    )


class TestDiagram:
    def test_negation_reaches_the_diagram(self):
        flow = _flow_with(
            Condition(left="$Record.AccountId", operator="IsNull",
                      right=Value(boolean_value=False))
        )
        diagram = to_mermaid(flow)
        assert "is not null" in diagram
        assert "AccountId is null" not in diagram

    def test_negation_reaches_the_documentation(self):
        flow = _flow_with(
            Condition(left="$Record.AccountId", operator="IsNull",
                      right=Value(boolean_value=False))
        )
        assert "is not null" in to_markdown(flow)


def _flow_with_transform_action(action: TransformValueAction) -> Flow:
    return Flow(
        api_name="F", label="F", start=Start(next="T"),
        elements=[
            Transform(
                name="T", label="Build", object_type="Account",
                transform_values=[TransformValue(actions=[action])],
            ),
        ],
    )


class TestTransform:
    """
    Map is understood; the other five action types round-trip through
    input_parameters with no confirmed shape - the diagram has to say so,
    rather than presenting a guess as settled fact.
    """

    def test_a_map_action_carries_no_disclaimer(self):
        flow = _flow_with_transform_action(
            TransformValueAction(transform_type="Map", output_field_api_name="Name",
                                  value=Value(string_value="Acme"))
        )
        assert "not fully confirmed" not in to_mermaid(flow)
        assert "never been confirmed" not in to_markdown(flow)

    def test_an_unmodelled_action_type_carries_a_disclaimer(self):
        flow = _flow_with_transform_action(
            TransformValueAction(
                transform_type="Sum",
                input_parameters=[
                    InputAssignment(name="sourceCollectionReference",
                                     value=Value(element_reference="col")),
                ],
            )
        )
        assert "shape not fully confirmed" in to_mermaid(flow)
        assert "never been confirmed against a live org" in to_markdown(flow)


class TestFaultPaths:
    def test_a_fault_path_is_drawn(self):
        # Error handling the user cannot see is error handling they cannot
        # approve, and the diagram is the approval screen.
        flow = Flow(
            api_name="F", label="F", start=Start(next="Get"),
            elements=[
                GetRecords(name="Get", label="Get", object="Account",
                           fault_next="Log"),
                RecordCreate(name="Log", label="Log", object="Task",
                             fields=[FieldValue(field="Subject",
                                                value=Value(string_value="x"))]),
            ],
        )
        diagram = to_mermaid(flow)
        assert 'Get -.->|"on error"| Log' in diagram


class TestLabels:
    def test_newlines_become_line_breaks(self):
        # A real newline inside a Mermaid label ends the label and breaks the parse.
        diagram = to_mermaid(_flow_with(
            Condition(left="$Record.Amount", operator="GreaterThan",
                      right=Value(number_value=1))
        ))
        for line in diagram.splitlines():
            assert "\n" not in line
        assert "<br/>" in diagram

    def test_quotes_are_escaped(self):
        diagram = to_mermaid(_flow_with(
            Condition(left="$Record.Stage", operator="EqualTo",
                      right=Value(string_value='Closed "Won"'))
        ))
        assert "#quot;" in diagram

    def test_a_path_that_ends_gets_an_end_node(self):
        # The IR has no End element; the diagram needs one so the reader can see
        # where a path stops.
        diagram = to_mermaid(_flow_with(
            Condition(left="$Record.Amount", operator="GreaterThan",
                      right=Value(number_value=1))
        ))
        assert "FLOW_END" in diagram
