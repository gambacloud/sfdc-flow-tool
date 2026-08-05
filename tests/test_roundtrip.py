"""
IR -> XML -> IR must return what it started with.

This is the property the whole design rests on. If the compiler and the parser
disagree, then the diagram shown for an existing flow describes something other
than what is in the org, and editing that flow would deploy the difference.
"""

import pytest

from flowtool.ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    Condition,
    Decision,
    FieldValue,
    Flow,
    GetRecords,
    InputAssignment,
    Loop,
    Outcome,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Start,
    Subflow,
    Value,
    Variable,
)
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate


def roundtrip(flow: Flow) -> Flow:
    return parse_flow(generate(flow), api_name=flow.api_name)


def assert_survives(flow: Flow) -> None:
    returned = roundtrip(flow)
    assert returned.model_dump() == flow.model_dump()


# --------------------------------------------------------------------------
# Fixtures, one per element type
# --------------------------------------------------------------------------


def _flow(*elements, start: Start = None, **kwargs) -> Flow:
    return Flow(
        api_name="Round_Trip",
        label="Round Trip",
        start=start or Start(next=elements[0].name),
        elements=list(elements),
        **kwargs,
    )


class TestElements:
    def test_assignment(self):
        assert_survives(_flow(
            Assignment(
                name="Set_Count", label="Set count",
                items=[
                    AssignmentItem(to_reference="v_Count", value=Value(number_value=0)),
                    AssignmentItem(to_reference="v_Name", operator="Add",
                                   value=Value(string_value="x")),
                ],
            )
        ))

    def test_decision_with_two_outcomes(self):
        assert_survives(_flow(
            Decision(
                name="Check", label="Check",
                outcomes=[
                    Outcome(
                        name="Big", label="Big",
                        conditions=[
                            Condition(left="$Record.Amount", operator="GreaterThan",
                                      right=Value(number_value=10000)),
                            Condition(left="$Record.StageName", operator="EqualTo",
                                      right=Value(string_value="Closed Won")),
                        ],
                        condition_logic="and",
                        next="Done",
                    ),
                    Outcome(
                        name="Missing_Account", label="No account",
                        conditions=[
                            Condition(left="$Record.AccountId", operator="IsNull",
                                      right=Value(boolean_value=True)),
                        ],
                        next=None,
                    ),
                ],
                default_outcome_label="Otherwise",
                next="Done",
            ),
            GetRecords(name="Done", label="Get", object="Account"),
        ))

    def test_get_records_with_filters_and_sort(self):
        assert_survives(_flow(
            GetRecords(
                name="Get_Contacts", label="Get contacts", object="Contact",
                filters=[
                    RecordFilter(field="AccountId", operator="EqualTo",
                                 value=Value(element_reference="$Record.AccountId")),
                    RecordFilter(field="Email", operator="IsNull",
                                 value=Value(boolean_value=False)),
                ],
                filter_logic="and",
                first_record_only=False,
                store_output_automatically=True,
                sort_field="CreatedDate",
                sort_order="Desc",
            )
        ))

    def test_record_create_from_fields(self):
        assert_survives(_flow(
            RecordCreate(
                name="Make_Task", label="Make task", object="Task",
                fields=[
                    FieldValue(field="Subject", value=Value(string_value="Follow up")),
                    FieldValue(field="WhatId",
                               value=Value(element_reference="$Record.AccountId")),
                ],
            )
        ))

    def test_record_create_from_a_variable(self):
        # No object: the variable carries it, and the XML has no <object> to
        # read back, so holding one in the IR would break the round trip.
        assert_survives(_flow(
            RecordCreate(name="Make", label="Make", input_reference="v_NewTask")
        ))

    def test_record_update_by_criteria(self):
        assert_survives(_flow(
            RecordUpdate(
                name="Mark_Hot", label="Mark hot", object="Account",
                filters=[RecordFilter(field="Id", operator="EqualTo",
                                      value=Value(element_reference="$Record.AccountId"))],
                fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
            )
        ))

    def test_record_update_by_reference(self):
        assert_survives(_flow(
            RecordUpdate(name="Save", label="Save", input_reference="Get_Account")
        ))

    def test_record_update_by_reference_with_field_values(self):
        # Flow Builder's third mode, and what real record-triggered flows use to
        # write back to $Record. An org flow using it is what caught this.
        assert_survives(_flow(
            RecordUpdate(
                name="Update_Trigger_Record", label="Update this record",
                input_reference="$Record",
                fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
            )
        ))

    def test_record_delete(self):
        assert_survives(_flow(
            RecordDelete(name="Remove", label="Remove", object="Task",
                         filters=[RecordFilter(field="IsClosed", operator="EqualTo",
                                               value=Value(boolean_value=True))])
        ))

    def test_loop(self):
        assert_survives(_flow(
            Loop(name="For_Each", label="For each", collection_reference="v_Items",
                 iteration_order="Desc", first_element="Body", next=None),
            GetRecords(name="Body", label="Body", object="Account", next="For_Each"),
        ))

    def test_action_call_email_alert(self):
        assert_survives(_flow(
            ActionCall(
                name="Notify_Owner", label="Notify the owner",
                action_name="High_Value_Deal_Alert", action_type="emailAlert",
                input_parameters=[
                    InputAssignment(name="SObjectRowId",
                                    value=Value(element_reference="$Record.Id")),
                ],
            )
        ))

    def test_action_call_apex_invocable_with_a_fault_path(self):
        assert_survives(_flow(
            ActionCall(
                name="Sync_To_ERP", label="Sync to ERP",
                action_name="ERPSyncInvocable", action_type="apex",
                input_parameters=[
                    InputAssignment(name="recordIds",
                                    value=Value(element_reference="$Record.Id")),
                    InputAssignment(name="mode", value=Value(string_value="upsert")),
                ],
                store_output_automatically=True,
                fault_next="Log_It",
            ),
            RecordCreate(
                name="Log_It", label="Log the failure", object="Task",
                fields=[FieldValue(field="Subject", value=Value(string_value="Sync failed"))],
            ),
        ))

    def test_a_fault_path_counts_as_reaching_an_element(self):
        # An element only reachable through a fault connector is still reachable;
        # treating it as an orphan would refuse a perfectly good flow.
        flow = _flow(
            ActionCall(name="Do_It", label="Do it", action_name="X", action_type="apex",
                       fault_next="Handle"),
            RecordCreate(name="Handle", label="Handle", object="Task",
                         fields=[FieldValue(field="Subject",
                                            value=Value(string_value="failed"))]),
        )
        assert "Handle" in flow.reachable()

    def test_subflow(self):
        assert_survives(_flow(
            Subflow(
                name="Call_It", label="Call it", flow_name="Some_Other_Flow",
                input_assignments=[
                    InputAssignment(name="inputId",
                                    value=Value(element_reference="$Record.Id")),
                ],
            )
        ))


class TestFlowLevel:
    def test_record_triggered_start(self):
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            start=Start(
                object="Opportunity", record_trigger_type="CreateAndUpdate",
                trigger_type="RecordAfterSave",
                filters=[RecordFilter(field="StageName", operator="EqualTo",
                                      value=Value(string_value="Closed Won"))],
                next="Get",
            ),
        ))

    def test_autolaunched_start(self):
        assert_survives(_flow(GetRecords(name="Get", label="Get", object="Account")))

    def test_description_and_status(self):
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            description="Does a thing.",
            status="Active",
            api_version="67.0",
        ))

    def test_variables(self):
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            variables=[
                Variable(name="v_Count", data_type="Number"),
                Variable(name="v_Accounts", data_type="SObject", is_collection=True,
                         is_input=True, is_output=True, object_type="Account"),
            ],
        ))

    def test_typed_values_keep_their_type(self):
        """A number that comes back as a string would change the flow's meaning."""
        flow = _flow(
            Decision(
                name="D", label="D",
                outcomes=[Outcome(
                    name="Yes", label="Yes",
                    conditions=[
                        Condition(left="a", operator="EqualTo", right=Value(number_value=42)),
                        Condition(left="b", operator="EqualTo", right=Value(boolean_value=True)),
                        Condition(left="c", operator="EqualTo", right=Value(string_value="42")),
                    ],
                    next=None,
                )],
            )
        )
        conditions = roundtrip(flow).elements[0].outcomes[0].conditions
        assert conditions[0].right.number_value == 42
        assert conditions[1].right.boolean_value is True
        assert conditions[2].right.string_value == "42"


class TestUnsupported:
    """Anything the IR cannot hold must be refused, never silently dropped."""

    def _xml(self, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            f"{body}"
            "<start><connector><targetReference>A</targetReference></connector></start>"
            "</Flow>"
        )

    @pytest.mark.parametrize("tag,expected", [
        ("screens", "screen elements"),
        ("waits", "wait / pause elements"),
        ("formulas", "formula resources"),
        ("collectionProcessors", "collection filter/sort"),
    ])
    def test_named_constructs_are_refused(self, tag, expected):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._xml(f"<{tag}><name>A</name></{tag}>"))
        assert expected in str(caught.value)

    def test_an_unknown_tag_is_still_refused(self):
        with pytest.raises(UnsupportedFlow, match="somethingNew"):
            parse_flow(self._xml("<somethingNew><name>A</name></somethingNew>"))

    def test_screen_flows_are_refused(self):
        xml = self._xml("").replace("AutoLaunchedFlow", "Flow")
        with pytest.raises(UnsupportedFlow, match="process type Flow"):
            parse_flow(xml)

    def test_every_reason_is_listed_not_just_the_first(self):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._xml(
                "<screens><name>A</name></screens><waits><name>B</name></waits>"
            ))
        assert len(caught.value.reasons) == 2

    def test_malformed_xml_is_refused(self):
        with pytest.raises(UnsupportedFlow, match="did not parse"):
            parse_flow("<Flow><unclosed>")

    def test_an_element_the_ir_rejects_is_reported_not_crashed(self):
        """
        A deployed flow that the IR refuses means the IR is stricter than
        Salesforce. That is a gap to report, not a traceback - it reached the
        user as a 500 the first time an org flow hit it.
        """
        body = (
            "<recordUpdates><name>Update_It</name><label>Update</label>"
            "<inputReference>$Record</inputReference>"
            "<filters><field>Id</field><operator>EqualTo</operator>"
            "<value><stringValue>x</stringValue></value></filters>"
            "</recordUpdates>"
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._xml(body))
        assert "Update_It does not fit" in str(caught.value)
