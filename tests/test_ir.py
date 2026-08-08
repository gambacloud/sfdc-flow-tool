"""The IR's job is to make invalid flows unrepresentable. These pin that down."""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    RecordFilter,
    Loop,
    Assignment,
    AssignmentItem,
    Condition,
    Decision,
    FieldValue,
    Flow,
    GetRecords,
    Outcome,
    RecordUpdate,
    Start,
    Value,
)


def _flow(**overrides):
    defaults = dict(
        api_name="Test_Flow",
        label="Test Flow",
        start=Start(next="Assign"),
        elements=[
            Assignment(
                name="Assign",
                label="Assign",
                items=[AssignmentItem(to_reference="v_Count", value=Value(number_value=0))],
            )
        ],
    )
    defaults.update(overrides)
    return Flow(**defaults)


class TestApiNames:
    @pytest.mark.parametrize("name", ["Get_Account", "A", "Step_1_Of_2"])
    def test_accepts_valid(self, name):
        assert GetRecords(name=name, label="x", object="Account").name == name

    @pytest.mark.parametrize("name", ["New Customer", "1_First", "trailing_", "double__under"])
    def test_rejects_invalid(self, name):
        # This is the class of bug that produced <name>New Customer</name>.
        with pytest.raises(ValidationError, match="not a valid Salesforce API name"):
            GetRecords(name=name, label="x", object="Account")


class TestRecordUpdateModes:
    def test_input_reference_alone_is_fine(self):
        RecordUpdate(name="U", label="U", input_reference="Get_Account")

    def test_criteria_mode_is_fine(self):
        RecordUpdate(
            name="U",
            label="U",
            object="Account",
            fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
        )

    def test_reference_plus_fields_is_a_real_shape(self):
        # Flow Builder's third mode: take the record from a reference and set
        # fields on it. Deployed flows in real orgs use this with $Record, so
        # forbidding it made existing flows unreadable.
        RecordUpdate(
            name="U",
            label="U",
            input_reference="$Record",
            fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
        )

    def test_reference_plus_filters_is_rejected(self):
        # The reference already picked the records; filters would pick them again.
        with pytest.raises(ValidationError, match="cannot be combined with filters"):
            RecordUpdate(
                name="U",
                label="U",
                input_reference="$Record",
                filters=[RecordFilter(field="Id", operator="EqualTo",
                                      value=Value(string_value="x"))],
            )

    def test_needs_some_target(self):
        with pytest.raises(ValidationError, match="input_reference or object"):
            RecordUpdate(name="U", label="U")


class TestValue:
    def test_requires_exactly_one_field(self):
        with pytest.raises(ValidationError, match="exactly one field"):
            Value()
        with pytest.raises(ValidationError, match="exactly one field"):
            Value(string_value="a", number_value=1)


class TestConditions:
    def test_binary_operator_needs_a_right_hand_side(self):
        with pytest.raises(ValidationError, match="requires a right-hand value"):
            Condition(left="$Record.Amount", operator="GreaterThan")

    def test_unary_operator_does_not(self):
        Condition(left="$Record.AccountId", operator="IsNull")


class TestReferences:
    def test_dangling_next_is_rejected(self):
        # The bug class behind <targetReference>End_1</targetReference>.
        with pytest.raises(ValidationError, match="unresolved references"):
            _flow(
                elements=[
                    Assignment(
                        name="Assign",
                        label="Assign",
                        next="Nowhere",
                        items=[AssignmentItem(to_reference="v", value=Value(number_value=1))],
                    )
                ]
            )

    def test_ending_a_path_is_not_a_dangling_reference(self):
        _flow()  # start -> Assign, Assign.next is None

    def test_dangling_outcome_is_rejected(self):
        with pytest.raises(ValidationError, match="unresolved references"):
            _flow(
                start=Start(next="D"),
                elements=[
                    Decision(
                        name="D",
                        label="D",
                        outcomes=[
                            Outcome(
                                name="Yes",
                                label="Yes",
                                conditions=[
                                    Condition(
                                        left="$Record.Amount",
                                        operator="GreaterThan",
                                        right=Value(number_value=1),
                                    )
                                ],
                                next="Missing",
                            )
                        ],
                    )
                ],
            )

    def test_start_must_connect_to_something(self):
        # Salesforce: "The flow can't run because nothing is connected to the
        # Start element." Caught here instead of in a deploy.
        with pytest.raises(ValidationError, match="nothing is connected to the Start"):
            _flow(start=Start())

    def test_an_empty_flow_needs_no_start_connector(self):
        _flow(start=Start(), elements=[])

    def test_unreachable_elements_are_warned_about_not_rejected(self):
        """
        Salesforce deploys a flow with an element nothing reaches, and one of
        its own sample apps ships a live Active flow that has one. Refusing it
        made that flow impossible to open, which is worse than drawing it with
        a note on top. The model is still told, because a forgotten connector
        on a flow it just wrote is what this check is for.
        """
        flow = _flow(
                start=Start(next="Assign"),
                elements=[
                    Assignment(
                        name="Assign",
                        label="Assign",
                        items=[AssignmentItem(to_reference="v", value=Value(number_value=1))],
                    ),
                    GetRecords(name="Orphan", label="Orphan", object="Account"),
                ],
            )
        assert "unreachable elements" in "\n".join(flow.warnings())

    def test_a_fully_connected_flow_has_nothing_to_warn_about(self):
        flow = _flow(
            start=Start(next="Assign"),
            elements=[
                Assignment(
                    name="Assign", label="Assign",
                    items=[AssignmentItem(to_reference="v",
                                          value=Value(number_value=1))],
                ),
            ],
        )
        assert flow.warnings() == []

    def test_the_unreachable_warning_shows_where_the_chain_breaks(self):
        """
        The model repaired nothing across four attempts when the message only
        said "connect them". It needs to see the wiring to find the gap.
        """
        flow = _flow(
                start=Start(next="D"),
                elements=[
                    Decision(
                        name="D",
                        label="D",
                        outcomes=[
                            Outcome(
                                name="Yes",
                                label="Yes",
                                conditions=[
                                    Condition(
                                        left="$Record.Amount",
                                        operator="GreaterThan",
                                        right=Value(number_value=1),
                                    )
                                ],
                            )
                        ],
                    ),
                    GetRecords(name="Orphan", label="x", object="Account"),
                ],
            )
        message = "\n".join(flow.warnings())
        assert "start -> D" in message
        assert "D outcome Yes -> (ends)" in message, "must show the broken link"
        assert "Orphan -> (ends)" in message

    def test_the_warning_names_the_reachable_paths_that_stop(self):
        """
        The connector map says what is stranded but not where the loose end is.
        A repair that cannot find the loose end just re-emits the same flow, so
        the reachable elements whose path stops are named outright - the missing
        connector belongs to one of them.
        """
        flow = _flow(
                start=Start(next="First"),
                elements=[
                    GetRecords(name="First", label="First", object="Account"),
                    GetRecords(name="Orphan", label="Orphan", object="Contact"),
                ],
            )
        message = "\n".join(flow.warnings())
        assert "Paths that currently stop: ['First']" in message
        assert "Orphan" not in message.split("Paths that currently stop")[1], (
            "an unreachable element is not a loose end to connect from"
        )

    def test_elements_reached_through_a_decision_outcome_count(self):
        _flow(
            start=Start(next="D"),
            elements=[
                Decision(
                    name="D",
                    label="D",
                    outcomes=[
                        Outcome(
                            name="Yes",
                            label="Yes",
                            conditions=[
                                Condition(
                                    left="$Record.Amount",
                                    operator="GreaterThan",
                                    right=Value(number_value=1),
                                )
                            ],
                            next="Only_Via_Outcome",
                        )
                    ],
                ),
                GetRecords(name="Only_Via_Outcome", label="x", object="Account"),
            ],
        )

    def test_elements_reached_through_a_loop_body_count(self):
        _flow(
            start=Start(next="L"),
            elements=[
                Loop(name="L", label="L", collection_reference="v_Items",
                     first_element="In_Body"),
                GetRecords(name="In_Body", label="x", object="Account", next="L"),
            ],
        )

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate element names"):
            _flow(
                elements=[
                    GetRecords(name="Dup", label="a", object="Account"),
                    GetRecords(name="Dup", label="b", object="Contact"),
                ],
                start=Start(next="Dup"),
            )


class TestStart:
    def test_record_triggered_needs_full_trigger_config(self):
        with pytest.raises(ValidationError, match="requires trigger_type"):
            Start(object="Opportunity")

    def test_autolaunched_needs_nothing(self):
        Start(next="Assign")
