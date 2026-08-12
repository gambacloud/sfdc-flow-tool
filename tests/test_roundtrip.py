"""
IR -> XML -> IR must return what it started with.

This is the property the whole design rests on. If the compiler and the parser
disagree, then the diagram shown for an existing flow describes something other
than what is in the org, and editing that flow would deploy the difference.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    CollectionFilter,
    CollectionSort,
    CollectionSortOption,
    ComponentOutput,
    Condition,
    Constant,
    DataTypeMapping,
    Decision,
    FieldValue,
    Flow,
    Formula,
    GetRecords,
    InputAssignment,
    Loop,
    Outcome,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Schedule,
    Start,
    Subflow,
    SubflowOutputAssignment,
    TextTemplate,
    Transform,
    TransformValue,
    TransformValueAction,
    Value,
    Variable,
)
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate


def roundtrip(flow: Flow) -> Flow:
    return parse_flow(generate(flow), api_name=flow.api_name)


def assert_survives(flow: Flow) -> None:
    """
    Every field must come back identical. Element *order* is not compared:
    control flow comes from connectors, and the XML groups elements by tag, so
    the order that comes back is an artefact of the format rather than anything
    the flow means. Everything else is compared exactly, per element, by name.
    """
    returned = roundtrip(flow)

    before, after = flow.model_dump(), returned.model_dump()
    assert {k: v for k, v in after.items() if k != "elements"} == {
        k: v for k, v in before.items() if k != "elements"
    }

    by_name = {e["name"]: e for e in after["elements"]}
    assert set(by_name) == {e["name"] for e in before["elements"]}, "elements lost or added"
    for element in before["elements"]:
        assert by_name[element["name"]] == element, f"{element['name']} changed"


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

    def test_subflow_with_manually_assigned_outputs(self):
        assert_survives(_flow(
            Subflow(
                name="Call_It", label="Call it", flow_name="Some_Other_Flow",
                output_assignments=[
                    SubflowOutputAssignment(name="OutputId",
                                            assign_to_reference="v_NewId"),
                    SubflowOutputAssignment(name="OutputStatus",
                                            assign_to_reference="v_Status"),
                ],
            )
        ))

    def test_subflow_with_automatic_output_storage(self):
        assert_survives(_flow(
            Subflow(
                name="Call_It", label="Call it", flow_name="Some_Other_Flow",
                store_output_automatically=True,
            )
        ))

    def test_collection_filter(self):
        assert_survives(_flow(
            CollectionFilter(
                name="Filter", label="Filter",
                collection_reference="ContactRoles",
                current_item="currentItem_Filter",
                conditions=[
                    Condition(left="currentItem_Filter.Role", operator="EqualTo",
                             right=Value(string_value="Decision Maker")),
                ],
            )
        ))

    def test_collection_sort(self):
        assert_survives(_flow(
            CollectionSort(
                name="Sort_Contact_Roles", label="Sort Contact Roles",
                collection_reference="ContactRoles",
                sort_options=[
                    CollectionSortOption(sort_field="LastModifiedDate", sort_order="Asc"),
                ],
            )
        ))

    def test_a_real_orgs_filter_and_sort_parse_as_expected(self):
        """
        Not a round trip - real XML retrieved from an org (via
        toddhalfpenny/salesforce-flow-visualiser's test fixtures), parsed
        directly, to check the reader's assumptions against something this
        tool did not itself produce.
        """
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>59.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<collectionProcessors>"
            "<name>Filter</name>"
            "<elementSubtype>FilterCollectionProcessor</elementSubtype>"
            "<label>Filter</label><locationX>176</locationX><locationY>539</locationY>"
            "<assignNextValueToReference>currentItem_Filter</assignNextValueToReference>"
            "<collectionProcessorType>FilterCollectionProcessor</collectionProcessorType>"
            "<collectionReference>ContactRoles</collectionReference>"
            "<conditionLogic>and</conditionLogic>"
            "<conditions><leftValueReference>currentItem_Filter.Role</leftValueReference>"
            "<operator>EqualTo</operator><rightValue><stringValue>Decision Maker</stringValue>"
            "</rightValue></conditions>"
            "</collectionProcessors>"
            "<collectionProcessors>"
            "<name>Sort_Contact_Roles</name>"
            "<elementSubtype>SortCollectionProcessor</elementSubtype>"
            "<label>Sort Contact Roles</label><locationX>176</locationX><locationY>431</locationY>"
            "<collectionProcessorType>SortCollectionProcessor</collectionProcessorType>"
            "<collectionReference>ContactRoles</collectionReference>"
            "<connector><targetReference>Filter</targetReference></connector>"
            "<sortOptions><doesPutEmptyStringAndNullFirst>false</doesPutEmptyStringAndNullFirst>"
            "<sortField>LastModifiedDate</sortField><sortOrder>Asc</sortOrder></sortOptions>"
            "</collectionProcessors>"
            "<start><connector><targetReference>Sort_Contact_Roles</targetReference>"
            "</connector></start>"
            "</Flow>"
        )
        flow = parse_flow(xml, api_name="X")
        by_name = {e.name: e for e in flow.elements}
        sort = by_name["Sort_Contact_Roles"]
        assert isinstance(sort, CollectionSort)
        assert sort.collection_reference == "ContactRoles"
        assert sort.next == "Filter"
        assert sort.sort_options == [
            CollectionSortOption(sort_field="LastModifiedDate", sort_order="Asc",
                                 does_put_empty_string_and_null_first=False)
        ]

        filt = by_name["Filter"]
        assert isinstance(filt, CollectionFilter)
        assert filt.current_item == "currentItem_Filter"
        assert filt.conditions[0].left == "currentItem_Filter.Role"

    def test_transform_maps_fields_from_variables(self):
        # Verified end to end against a real dev org's checkOnly validation
        # (`sf project deploy start --dry-run`), not just the schema: this
        # exact shape - output_field_api_name per action, no name on the
        # TransformValue itself - is what the org actually accepts for Map.
        assert_survives(_flow(
            Transform(
                name="Build_Account", label="Build account",
                object_type="Account",
                transform_values=[
                    TransformValue(
                        actions=[TransformValueAction(
                            transform_type="Map",
                            output_field_api_name="Name",
                            value=Value(element_reference="v_Name"),
                        )],
                    ),
                    TransformValue(
                        actions=[TransformValueAction(
                            transform_type="Map",
                            output_field_api_name="BillingCity",
                            value=Value(string_value="Springfield"),
                        )],
                    ),
                ],
            )
        ))

    def test_a_transform_values_name_is_rejected_unless_it_is_an_inner_join(self):
        # The org's own words, from a real checkOnly deploy: "The flow
        # metadata specifies 'Name' for the name of a transformValue, which is
        # supported only if transformType is InnerJoin."
        with pytest.raises(ValidationError, match="InnerJoin"):
            TransformValue(
                name="Name",
                actions=[TransformValueAction(
                    transform_type="Map", value=Value(string_value="x"),
                )],
            )

    def test_transform_with_an_unmodelled_action_type_still_round_trips(self):
        # Sum/Count/GetItemByIndex/InnerJoin/InvocableAction: no confirmed
        # example of any of their individual shapes, so they round-trip
        # through input_parameters rather than being guessed at.
        assert_survives(_flow(
            Transform(
                name="Summarise", label="Summarise", object_type="Account",
                is_collection=False,
                transform_values=[
                    TransformValue(
                        actions=[TransformValueAction(
                            name="Sum_Amounts",
                            transform_type="Sum",
                            output_field_api_name="AnnualRevenue",
                            input_parameters=[
                                InputAssignment(
                                    name="sourceCollectionReference",
                                    value=Value(element_reference="col_Opportunities"),
                                ),
                                InputAssignment(
                                    name="sourceField",
                                    value=Value(string_value="Amount"),
                                ),
                            ],
                        )],
                    ),
                ],
            )
        ))

    def test_action_call_with_data_type_mappings(self):
        # A generic Apex-typed action, pinned to a concrete SObject for this call.
        assert_survives(_flow(
            ActionCall(
                name="Generate_Report", label="Generate report",
                action_name="GenerateCollectionReport", action_type="apex",
                data_type_mappings=[
                    DataTypeMapping(type_name="T__inputRecord", type_value="Account"),
                    DataTypeMapping(type_name="T__inputRecordCollection",
                                    type_value="Account"),
                ],
            )
        ))

    def test_action_call_with_manually_assigned_outputs(self):
        assert_survives(_flow(
            ActionCall(
                name="Geocode", label="Geocode",
                action_name="geocodeAddress", action_type="apex",
                output_parameters=[
                    ComponentOutput(name="lat", assign_to_reference="v_Lat"),
                    ComponentOutput(name="lng", assign_to_reference="v_Lng"),
                ],
            )
        ))

    def test_action_call_cannot_mix_automatic_and_manual_outputs(self):
        with pytest.raises(ValidationError, match="storeOutputAutomatically"):
            ActionCall(
                name="Geocode", label="Geocode",
                action_name="geocodeAddress", action_type="apex",
                store_output_automatically=True,
                output_parameters=[
                    ComponentOutput(name="lat", assign_to_reference="v_Lat"),
                ],
            )

    def test_action_call_waits_for_an_async_action_with_a_timeout(self):
        # isWaitUntilCompleted/offset/offsetUnit/timeoutConnector: confirmed
        # against the org's own live Metadata API schema (describeValueType
        # on FlowActionCall), not found in any public sample flow.
        assert_survives(_flow(
            ActionCall(
                name="Call_External_Service", label="Call external service",
                action_name="externalServiceAction", action_type="externalService",
                is_wait_until_completed=True,
                timeout_offset=5, timeout_offset_unit="Minutes",
                timeout_next="Handle_Timeout",
            ),
            Assignment(name="Handle_Timeout", label="Handle timeout", items=[
                AssignmentItem(to_reference="v_TimedOut", value=Value(boolean_value=True)),
            ]),
        ))

    def test_action_call_timeout_needs_both_offset_and_unit(self):
        with pytest.raises(ValidationError, match="timeout_offset_unit"):
            ActionCall(
                name="Call_It", label="Call it",
                action_name="externalServiceAction", action_type="externalService",
                timeout_offset=5,
            )

    def test_the_orgs_own_offset_and_timeout_fields_parse_as_expected(self):
        """
        Not a round trip - the exact tag names and shapes as confirmed live
        against a connected dev org's Metadata API
        (describeValueType on FlowActionCall), parsed directly.
        """
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<actionCalls>"
            "<name>Call_External_Service</name><label>Call external service</label>"
            "<locationX>0</locationX><locationY>0</locationY>"
            "<actionName>externalServiceAction</actionName>"
            "<actionType>externalService</actionType>"
            "<isWaitUntilCompleted>true</isWaitUntilCompleted>"
            "<offset>5</offset><offsetUnit>Minutes</offsetUnit>"
            "<timeoutConnector><targetReference>Handle_Timeout</targetReference>"
            "</timeoutConnector>"
            "<connector><targetReference>Handle_Timeout</targetReference></connector>"
            "</actionCalls>"
            "<assignments><name>Handle_Timeout</name><label>Handle timeout</label>"
            "<assignmentItems><assignToReference>v</assignToReference>"
            "<operator>Assign</operator><value><booleanValue>true</booleanValue>"
            "</value></assignmentItems></assignments>"
            "<start><connector><targetReference>Call_External_Service</targetReference>"
            "</connector></start>"
            "</Flow>"
        )
        flow = parse_flow(xml, api_name="X")
        call = flow.by_name()["Call_External_Service"]
        assert call.is_wait_until_completed is True
        assert call.timeout_offset == 5
        assert call.timeout_offset_unit == "Minutes"
        assert call.timeout_next == "Handle_Timeout"


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

    @pytest.mark.parametrize("frequency", ["Once", "Daily", "Weekly"])
    def test_a_scheduled_start(self, frequency):
        # Verified end to end against a real dev org's checkOnly validation.
        # Monthly, Yearly, Hourly, Weekdays and OnActivate are in the org's
        # own enum for this field but were all refused when actually
        # deployed - see the Schedule docstring in ir.py.
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            start=Start(
                trigger_type="Scheduled",
                schedule=Schedule(start_date="2026-08-15",
                                  start_time="02:00:00.000Z", frequency=frequency),
                next="Get",
            ),
        ))

    def test_a_scheduled_trigger_needs_a_schedule(self):
        # The org's own words: "You set the flow trigger type to Scheduled,
        # so you must also set the frequency."
        with pytest.raises(ValidationError, match="Scheduled"):
            Start(trigger_type="Scheduled", next="Get")

    def test_a_schedule_needs_a_scheduled_trigger(self):
        with pytest.raises(ValidationError, match="Scheduled"):
            Start(
                trigger_type="RecordAfterSave", object="Account",
                record_trigger_type="Update",
                schedule=Schedule(start_date="2026-08-15",
                                  start_time="02:00:00.000Z", frequency="Daily"),
                next="Get",
            )

    def test_a_filter_formula(self):
        # Verified against a real dev org's checkOnly validation: works
        # alongside structured filters too, not just in place of them.
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            start=Start(
                object="Opportunity", record_trigger_type="Update",
                trigger_type="RecordAfterSave",
                filter_formula='{!$Record.Amount} > 1000',
                next="Get",
            ),
        ))

    def test_a_filter_formula_needs_a_record_trigger(self):
        # Confirmed against a real dev org: a filterFormula on a
        # non-record-triggered start (e.g. Scheduled) doesn't get a clean
        # rejection from the org - it blows up with an opaque "unexpected
        # error" - so this is refused up front instead.
        with pytest.raises(ValidationError, match="record-triggered"):
            Start(
                trigger_type="Scheduled",
                schedule=Schedule(start_date="2026-08-15",
                                  start_time="02:00:00.000Z", frequency="Daily"),
                filter_formula="1 = 1",
                next="Get",
            )

    @pytest.mark.parametrize("flow_run_as_user", ["TriggeringUser", "DefaultWorkflowUser"])
    def test_flow_run_as_user(self, flow_run_as_user):
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account"),
            start=Start(
                object="Opportunity", record_trigger_type="Update",
                trigger_type="RecordAfterSave",
                flow_run_as_user=flow_run_as_user,
                next="Get",
            ),
        ))

    @pytest.mark.parametrize("trigger_type", ["RecordBeforeSave", "Scheduled"])
    def test_flow_run_as_user_needs_an_after_save_trigger(self, trigger_type):
        # The org's own words: "When the TriggerType field is set to
        # '<type>', the RunAsUser field isn't supported." - it deploys fine
        # and is silently ignored, which this IR refuses instead.
        kwargs = dict(flow_run_as_user="TriggeringUser", next="Get",
                      trigger_type=trigger_type)
        if trigger_type == "Scheduled":
            kwargs["schedule"] = Schedule(start_date="2026-08-15",
                                          start_time="02:00:00.000Z", frequency="Daily")
        else:
            kwargs.update(object="Opportunity", record_trigger_type="Update")
        with pytest.raises(ValidationError, match="RunAsUser"):
            Start(**kwargs)

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


class TestFaultPaths:
    """
    A fault connector used to be read as if it were not there, drawn as if it
    were not there, and deleted on the next deploy.
    """

    @pytest.mark.parametrize("element", [
        GetRecords(name="E", label="E", object="Account", next="H", fault_next="H"),
        RecordUpdate(name="E", label="E", object="Account",
                     fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
                     next="H", fault_next="H"),
        RecordDelete(name="E", label="E", object="Task", next="H", fault_next="H"),
        Subflow(name="E", label="E", flow_name="Other", next="H", fault_next="H"),
        ActionCall(name="E", label="E", action_name="A", action_type="apex",
                   next="H", fault_next="H"),
    ])
    def test_it_survives_the_round_trip(self, element):
        assert_survives(_flow(
            element,
            RecordCreate(name="H", label="Handle", object="Task",
                         fields=[FieldValue(field="Subject",
                                            value=Value(string_value="failed"))]),
        ))

    def test_it_reaches_the_xml(self):
        flow = _flow(
            GetRecords(name="Get", label="Get", object="Account", fault_next="H"),
            RecordCreate(name="H", label="Handle", object="Task",
                         fields=[FieldValue(field="Subject",
                                            value=Value(string_value="x"))]),
        )
        assert "<faultConnector>" in generate(flow)


class TestStartFlag:
    def test_only_when_changed_survives(self):
        # Decides whether the flow runs on every save or only on the transition.
        flow = _flow(
            GetRecords(name="Get", label="Get", object="Account"),
            start=Start(object="Account", record_trigger_type="Update",
                        trigger_type="RecordAfterSave",
                        only_when_changed_to_meet_criteria=True, next="Get"),
        )
        assert_survives(flow)
        assert "doesRequireRecordChangedToMeetCriteria" in generate(flow)


class TestElementDescription:
    def test_an_admins_note_is_not_deleted(self):
        assert_survives(_flow(
            GetRecords(name="Get", label="Get", object="Account",
                       description="Kept deliberately: see ticket SF-412."),
        ))


class TestNestedUnknownsAreRefused:
    """
    Checking only the root let anything nested through. Each of these was
    silently dropped before, then deleted on the next deploy.
    """

    def _flow_xml(self, body: str, start_extra: str = "") -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            f"{body}"
            "<start><connector><targetReference>E</targetReference></connector>"
            "<object>Account</object><recordTriggerType>Update</recordTriggerType>"
            f"<triggerType>RecordAfterSave</triggerType>{start_extra}</start>"
            "</Flow>"
        )

    def _lookup(self, extra: str = "") -> str:
        return (
            "<recordLookups><name>E</name><label>E</label>"
            f"<object>Account</object>{extra}</recordLookups>"
        )

    @pytest.mark.parametrize("extra,expected", [
        ("<limit>5</limit>", "a record limit"),
        ("<assignNextValueToReference>v</assignNextValueToReference>",
         "its own loop variable"),
        # outputAssignments is modelled now - see test_three_spellings.py.
        ("<limit>5</limit>", "a record limit"),
    ])
    def test_unknown_child_of_an_element(self, extra, expected):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._flow_xml(self._lookup(extra)))
        assert expected in str(caught.value)
        assert "E uses" in str(caught.value), "the message should name the element"

    @pytest.mark.parametrize("extra,expected", [
        # scheduledPaths, schedule and filterFormula used to be here. All are
        # modelled now - schedule's own children get the same allowlist
        # treatment, see test_scheduled_paths.py and TestSchedule below.
        ("<somethingMadeUp>x</somethingMadeUp>", "<somethingMadeUp>"),
    ])
    def test_unknown_child_of_the_start(self, extra, expected):
        with pytest.raises(UnsupportedFlow, match=expected):
            parse_flow(self._flow_xml(self._lookup(), start_extra=extra))

    def test_a_schedule_on_a_non_scheduled_trigger_is_rejected(self):
        # This base flow's trigger is RecordAfterSave; a schedule attached to
        # it is not a gap in the IR's coverage, it is the flow disagreeing
        # with itself.
        with pytest.raises(UnsupportedFlow, match="the trigger does not fit the model"):
            parse_flow(self._flow_xml(
                self._lookup(),
                start_extra=(
                    "<schedule><startDate>2026-08-15</startDate>"
                    "<startTime>02:00:00.000Z</startTime>"
                    "<frequency>Daily</frequency></schedule>"
                ),
            ))

    def test_unknown_child_of_a_variable(self):
        body = self._lookup() + (
            "<variables><name>v_X</name><dataType>String</dataType>"
            "<apexClass>Some.Type</apexClass></variables>"
        )
        with pytest.raises(UnsupportedFlow, match="an Apex type"):
            parse_flow(self._flow_xml(body))

    def test_a_variables_own_attributes_are_no_longer_unknown(self):
        """
        description, scale and a default value were the three most common
        blockers across public sample apps - and none of them is a feature.
        """
        body = self._lookup() + (
            "<variables><description>A note</description><name>v_X</name>"
            "<dataType>Number</dataType><scale>2</scale>"
            "<value><numberValue>0</numberValue></value></variables>"
        )
        flow = parse_flow(self._flow_xml(body))
        variable = flow.variables[0]
        assert variable.description == "A note"
        assert variable.scale == 2
        assert variable.value.number_value == 0

    def test_a_clean_element_still_parses(self):
        parse_flow(self._flow_xml(self._lookup()))

    def test_actionCalls_tolerates_the_orgs_own_nameSegment_echo(self):
        """
        xmlgen never writes nameSegment/versionSegment - the org adds them on
        save regardless, splitting actionName's value back out into its parts
        (e.g. "emailSimple" comes back with nameSegment "emailSimple"). A flow
        built and deployed by this tool therefore failed to re-import, purely
        from reading back what the org itself had added.
        """
        body = (
            "<actionCalls><name>E</name><label>E</label>"
            "<actionName>emailSimple</actionName><actionType>emailSimple</actionType>"
            "<nameSegment>emailSimple</nameSegment><versionSegment>1</versionSegment>"
            "</actionCalls>"
        )
        flow = parse_flow(self._flow_xml(body))
        assert flow.elements[0].action_name == "emailSimple"

    def test_every_supported_element_has_a_child_allowlist(self):
        # A new element type without one would silently ignore everything
        # inside it - the exact hole this closes. collectionProcessors is the
        # one exception: it is one tag wearing two shapes (Filter and Sort),
        # so it is checked separately against its own two allowlists instead
        # of the shared table.
        from flowtool.parse import _ELEMENT_CHILDREN, _READERS

        assert set(_READERS) == set(_ELEMENT_CHILDREN) | {"collectionProcessors"}

    def test_the_allowlists_cover_what_the_compiler_writes(self):
        """
        Anything xmlgen emits must be readable back, or the round trip breaks
        the moment that field is used.
        """
        import xml.etree.ElementTree as ET
        from flowtool.parse import _ELEMENT_CHILDREN
        from flowtool.xmlgen import METADATA_NS

        flow = _flow(
            GetRecords(name="Get", label="Get", object="Account",
                       description="note", next="Act", fault_next="Act",
                       filters=[RecordFilter(field="Id", operator="EqualTo",
                                             value=Value(string_value="x"))],
                       sort_field="Name", sort_order="Asc"),
            ActionCall(name="Act", label="Act", action_name="A", action_type="apex",
                       store_output_automatically=True,
                       input_parameters=[InputAssignment(
                           name="p", value=Value(string_value="v"))]),
        )
        root = ET.fromstring(generate(flow))
        problems = []
        for tag, allowed in _ELEMENT_CHILDREN.items():
            for node in root.findall(f"{{{METADATA_NS}}}{tag}"):
                for child in node:
                    name = child.tag.split("}")[-1]
                    if name not in allowed:
                        problems.append(f"{tag}.{name} is written but not readable")
        assert not problems, problems


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
        ("recordRollbacks", "rollback elements"),
        ("apexPluginCalls", "Apex plugin calls"),
    ])
    def test_named_constructs_are_refused(self, tag, expected):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._xml(f"<{tag}><name>A</name></{tag}>"))
        assert expected in str(caught.value)

    def test_a_collection_processor_of_an_unhandled_type_is_refused(self):
        # Filter and Sort are the two this build knows. A third type -
        # RecommendationMapCollectionProcessor is the one seen in the wild -
        # is named rather than silently misread as one of the two it does know.
        xml = self._xml(
            "<collectionProcessors><name>A</name>"
            "<collectionProcessorType>RecommendationMapCollectionProcessor</collectionProcessorType>"
            "</collectionProcessors>"
        )
        with pytest.raises(UnsupportedFlow, match="RecommendationMapCollectionProcessor"):
            parse_flow(xml)

    def test_an_unknown_tag_is_still_refused(self):
        with pytest.raises(UnsupportedFlow, match="somethingNew"):
            parse_flow(self._xml("<somethingNew><name>A</name></somethingNew>"))

    def test_migrated_workflow_rules_are_refused(self):
        # Deliberately out of scope: a legacy migration artefact nobody authors.
        xml = self._xml("").replace("AutoLaunchedFlow", "Workflow")
        with pytest.raises(UnsupportedFlow, match="process type Workflow"):
            parse_flow(xml)

    def test_every_reason_is_listed_not_just_the_first(self):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self._xml(
                "<recordRollbacks><name>A</name></recordRollbacks>"
                "<apexPluginCalls><name>B</name></apexPluginCalls>"
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


class TestVariableAttributes:
    """
    A survey of fifteen flows from Salesforce's own public sample apps found
    these three to be the top blockers - seen in 8, 7 and 6 flows. None of them
    is a feature: a note on a variable, its decimal places, and what it holds
    before anything runs. The whole flow was refused for a note.
    """

    def _with_variable(self, **fields) -> Flow:
        return Flow(
            api_name="Vars", label="Vars",
            start=Start(next="Get"),
            elements=[GetRecords(name="Get", label="Get", object="Account")],
            variables=[Variable(name="v_X", data_type="Number", **fields)],
        )

    def test_a_description(self):
        assert_survives(self._with_variable(description="What this holds."))

    def test_decimal_places(self):
        assert_survives(self._with_variable(scale=2))

    def test_a_literal_default(self):
        assert_survives(self._with_variable(value=Value(number_value=7)))

    def test_a_referenced_default(self):
        """
        Observed in the wild: a DateTime defaulting to $Flow.CurrentDateTime.
        A default is not always a literal, which is why it is a full Value.
        """
        assert_survives(Flow(
            api_name="Vars", label="Vars",
            start=Start(next="Get"),
            elements=[GetRecords(name="Get", label="Get", object="Account")],
            variables=[Variable(
                name="v_When", data_type="DateTime",
                value=Value(element_reference="$Flow.CurrentDateTime"),
            )],
        ))

    def test_all_three_together(self):
        assert_survives(self._with_variable(
            description="Running total.", scale=2, value=Value(number_value=0),
        ))

    def test_a_scale_of_zero_is_not_dropped(self):
        """0 is falsy, and `if var.scale:` would silently lose it."""
        flow = self._with_variable(scale=0)
        assert "<scale>0</scale>" in generate(flow)
        assert_survives(flow)

    def test_they_are_written_where_salesforce_writes_them(self):
        import xml.etree.ElementTree as ET
        from flowtool.xmlgen import METADATA_NS

        xml = generate(self._with_variable(
            description="note", scale=2, value=Value(number_value=1),
        ))
        node = ET.fromstring(xml).find(f"{{{METADATA_NS}}}variables")
        tags = [child.tag.split("}")[-1] for child in node]
        assert tags[0] == "description", "Salesforce emits the inherited fields first"
        assert tags[1] == "name"
        assert tags[-1] == "value"
        assert tags.index("scale") > tags.index("dataType")


class TestQueriedFields:
    """
    A Get Records may name the fields it fetches instead of taking all of them.
    Three of fifteen public sample-app flows do, and it was the sole remaining
    blocker on two of them.

    It sits alongside automatic storage rather than replacing it - the real
    metadata carries queriedFields and storeOutputAutomatically together, which
    is not what the names suggest.
    """

    def _get(self, **fields) -> Flow:
        return _flow(GetRecords(name="Get", label="Get", object="Booking__c", **fields))

    def test_a_chosen_field_list(self):
        assert_survives(self._get(queried_fields=["Id", "Experience_Name__c", "Date__c"]))

    def test_order_is_preserved(self):
        """The list is what the query asks for, in order, not a set."""
        flow = self._get(queried_fields=["Name", "Id", "Date__c"])
        assert roundtrip(flow).elements[0].queried_fields == ["Name", "Id", "Date__c"]

    def test_no_list_means_all_fields(self):
        flow = self._get()
        assert "<queriedFields>" not in generate(flow)
        assert roundtrip(flow).elements[0].queried_fields == []

    def test_it_coexists_with_automatic_storage(self):
        assert_survives(self._get(
            queried_fields=["Id", "Name"], store_output_automatically=True,
        ))

    def test_it_survives_alongside_everything_else_on_a_lookup(self):
        assert_survives(self._get(
            queried_fields=["Id", "Name"],
            filters=[RecordFilter(field="Id", operator="EqualTo",
                                  value=Value(string_value="x"))],
            first_record_only=False, sort_field="Name", sort_order="Desc",
            description="note",
        ))

    def test_it_is_written_where_salesforce_writes_it(self):
        import xml.etree.ElementTree as ET
        from flowtool.xmlgen import METADATA_NS

        xml = generate(self._get(queried_fields=["Id"], sort_field="Name",
                                 sort_order="Asc"))
        node = ET.fromstring(xml).find(f"{{{METADATA_NS}}}recordLookups")
        tags = [c.tag.split("}")[-1] for c in node]
        assert tags.index("object") < tags.index("queriedFields")
        assert tags.index("queriedFields") < tags.index("sortField")


class TestCreateRecordsOutput:
    """
    Two ways to get at the record just created, and they cannot be combined.
    Every rule here is the org's, quoted from what checkOnly said when the
    combination was tried.
    """

    FIELDS = [FieldValue(field="Name", value=Value(string_value="Test"))]

    def _create(self, **kw) -> Flow:
        return _flow(RecordCreate(name="Make", label="Make", **kw))

    def test_assigning_the_new_id_to_a_variable(self):
        assert_survives(self._create(
            object="Account", fields=self.FIELDS,
            assign_record_id_to_reference="v_NewId",
        ))

    def test_automatic_storage(self):
        assert_survives(self._create(object="Account", fields=self.FIELDS))

    def test_neither(self):
        assert_survives(self._create(
            object="Account", fields=self.FIELDS, store_output_automatically=False,
        ))

    def test_automatic_storage_off_is_not_quietly_turned_back_on(self):
        """
        storeOutputAutomatically was allowed through the allowlist but never
        read, while the compiler wrote it as true unconditionally. A Create with
        it switched off came back switched on - and would deploy that way.
        """
        xml = self._flow_xml(
            "<recordCreates><name>Make</name><label>Make</label>"
            "<assignRecordIdToReference>v_NewId</assignRecordIdToReference>"
            "<inputAssignments><field>Name</field>"
            "<value><stringValue>Test</stringValue></value></inputAssignments>"
            "<object>Account</object>"
            "</recordCreates>"
        )
        element = parse_flow(xml).elements[0]
        assert element.store_output_automatically is False
        assert element.assign_record_id_to_reference == "v_NewId"
        assert "<storeOutputAutomatically>" not in generate(parse_flow(xml))

    def _flow_xml(self, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            f"{body}"
            "<start><connector><targetReference>Make</targetReference></connector></start>"
            "</Flow>"
        )

    @pytest.mark.parametrize("kw,expected", [
        (dict(object="Account", assign_record_id_to_reference="v_Id",
              store_output_automatically=True), "storeOutputAutomatically"),
        (dict(input_reference="v_Rec", store_output_automatically=True),
         "storeOutputAutomatically"),
        (dict(input_reference="v_Rec", assign_record_id_to_reference="v_Id"),
         "sObjectInputReference"),
    ])
    def test_the_combinations_the_org_rejects_are_not_representable(self, kw, expected):
        from pydantic import ValidationError

        if "fields" not in kw and kw.get("object"):
            kw["fields"] = self.FIELDS
        with pytest.raises(ValidationError, match=expected):
            RecordCreate(name="Make", label="Make", **kw)

    @pytest.mark.parametrize("kw,expected", [
        (dict(object="Account", fields=FIELDS), True),
        (dict(object="Account", fields=FIELDS,
              assign_record_id_to_reference="v_Id"), False),
        (dict(input_reference="v_Rec"), False),
    ])
    def test_an_unset_flag_follows_the_shape(self, kw, expected):
        """
        A flag nobody set is not a decision. Requiring it would mean restating
        what the shape already implies, on every call.
        """
        assert RecordCreate(name="Make", label="Make", **kw).store_output_automatically \
            is expected


class TestGetRecordsOutput:
    """
    Manual storage puts the records in a variable instead of in the element's
    own output. It was the sole remaining blocker on one public sample-app flow.
    """

    def _get(self, **kw) -> Flow:
        return _flow(GetRecords(name="Get", label="Get", object="Account", **kw))

    def test_into_a_variable(self):
        assert_survives(self._get(output_reference="v_Accounts",
                                  first_record_only=False))

    def test_into_a_variable_with_named_fields(self):
        assert_survives(self._get(output_reference="v_Accounts",
                                  first_record_only=False,
                                  queried_fields=["Id", "Name"]))

    def test_automatic_storage_is_still_the_default(self):
        assert self._get().elements[0].store_output_automatically is True

    def test_the_flag_follows_the_shape(self):
        assert self._get(output_reference="v_A").elements[0] \
            .store_output_automatically is False

    def test_both_at_once_is_not_representable(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="one or the other"):
            GetRecords(name="Get", label="Get", object="Account",
                       output_reference="v_A", store_output_automatically=True)


class TestFirstRecordOnlyIsNotInvented:
    """
    Older flows omit getFirstRecordOnly and take the answer from the variable
    they store into. Reading that as `true` - the old default - would turn a
    query over many records into one over the first, and write it back that way.

    Both public flows that use manual storage omit it, and both store into
    collections, so this was live the moment output_reference landed.
    """

    XML = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
        "<apiVersion>62.0</apiVersion><label>X</label>"
        "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
        "<recordLookups><name>Get</name><label>Get</label><object>Account</object>"
        "<outputReference>v_Accounts</outputReference></recordLookups>"
        "<start><connector><targetReference>Get</targetReference></connector></start>"
        "</Flow>"
    )

    def test_an_omitted_flag_stays_unanswered(self):
        assert parse_flow(self.XML).elements[0].first_record_only is None

    def test_and_is_not_written_back(self):
        assert "<getFirstRecordOnly>" not in generate(parse_flow(self.XML))

    def test_a_stated_flag_is_kept(self):
        for stated in (True, False):
            xml = self.XML.replace(
                "<outputReference>",
                f"<getFirstRecordOnly>{str(stated).lower()}</getFirstRecordOnly>"
                "<outputReference>",
            )
            assert parse_flow(xml).elements[0].first_record_only is stated

    def test_a_flow_we_build_still_states_it(self):
        """Only a flow that never said stays silent; ours always say."""
        xml = generate(_flow(GetRecords(name="Get", label="Get", object="Account")))
        assert "<getFirstRecordOnly>true</getFirstRecordOnly>" in xml


class TestTextTemplates:
    """
    A block of text with merge fields, referenced by name. Sole remaining
    blocker on one public sample-app flow.
    """

    BODY = "Hello {!Name},\n\nYour order shipped.\n\nThanks,\nSupport"

    def _flow_with(self, *templates) -> Flow:
        return _flow(
            GetRecords(name="Get", label="Get", object="Account"),
            text_templates=list(templates),
        )

    def test_a_template(self):
        assert_survives(self._flow_with(TextTemplate(name="Note", text=self.BODY)))

    def test_plain_text_and_a_description(self):
        assert_survives(self._flow_with(TextTemplate(
            name="Note", text="Issued ${!amount} to {!count} customers.",
            is_viewed_as_plain_text=True, description="What happened.",
        )))

    def test_several(self):
        assert_survives(self._flow_with(
            TextTemplate(name="Found", text="Found {!n}."),
            TextTemplate(name="Missing", text="Nothing matched."),
        ))

    def test_it_shares_the_one_namespace(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="share one namespace"):
            _flow(
                GetRecords(name="Get", label="Get", object="Account"),
                text_templates=[TextTemplate(name="Get", text="x")],
            )


class TestBlankLinesAreContent:
    """
    The generator pretty-printed with minidom, which puts a blank line between
    elements, and then filtered every blank line back out. That filter could not
    tell a blank line between two tags from a paragraph break inside one - so an
    email template lost its paragraphs, and would have deployed that way.

    Text templates are what surfaced it, but two carriers were already exposed.
    """

    BODY = "First paragraph.\n\nSecond paragraph.\n\nThird."

    def test_in_a_text_template(self):
        flow = _flow(
            GetRecords(name="Get", label="Get", object="Account"),
            text_templates=[TextTemplate(name="Note", text=self.BODY)],
        )
        assert roundtrip(flow).text_templates[0].text == self.BODY

    def test_in_a_screens_display_text(self):
        from flowtool.ir import Screen, ScreenField

        flow = Flow(
            api_name="S", label="S", process_type="Flow", start=Start(next="Ask"),
            elements=[Screen(name="Ask", label="Ask", fields=[
                ScreenField(name="Intro", field_type="DisplayText",
                            field_text=self.BODY)])],
        )
        assert roundtrip(flow).elements[0].fields[0].field_text == self.BODY

    def test_in_an_elements_description(self):
        flow = _flow(GetRecords(name="Get", label="Get", object="Account",
                                description=self.BODY))
        assert roundtrip(flow).elements[0].description == self.BODY

    def test_the_xml_is_still_indented_and_has_no_stray_blank_lines(self):
        xml = generate(_flow(GetRecords(name="Get", label="Get", object="Account")))
        assert "\n    <recordLookups>" in xml, "still pretty-printed"
        assert "\n\n" not in xml, "no blank lines where there is no content"


class TestConstantsAndFormulas:
    """
    Two resources that always appeared together in the corpus, and neither of
    which freed a flow alone - so they were built together.
    """

    def _with(self, **kw) -> Flow:
        return _flow(GetRecords(name="Get", label="Get", object="Account"), **kw)

    def test_a_constant(self):
        assert_survives(self._with(constants=[
            Constant(name="Default_Rating", data_type="String",
                     value=Value(string_value="Warm")),
        ]))

    @pytest.mark.parametrize("data_type,value", [
        ("String", Value(string_value="x")),
        ("Number", Value(number_value=60)),
        ("Currency", Value(number_value=9.99)),
        ("Boolean", Value(boolean_value=True)),
        ("Date", Value(date_value="2026-01-01")),
        ("DateTime", Value(date_time_value="2026-01-01T09:00:00.000Z")),
    ])
    def test_every_constant_type(self, data_type, value):
        assert_survives(self._with(constants=[
            Constant(name="C", data_type=data_type, value=value),
        ]))

    def test_a_formula(self):
        assert_survives(self._with(formulas=[
            Formula(name="Vat", data_type="Currency",
                    expression="{!v_Total} * 0.2", scale=2),
        ]))

    def test_a_formula_with_no_scale_and_a_description(self):
        assert_survives(self._with(formulas=[
            Formula(name="Today_Is", data_type="Date", expression="TODAY()",
                    description="Right now, recomputed on each read."),
        ]))

    def test_both_together(self):
        assert_survives(self._with(
            constants=[Constant(name="Rate", data_type="Number",
                                value=Value(number_value=20))],
            formulas=[Formula(name="Vat", data_type="Currency",
                              expression="{!v_Total} * {!Rate} / 100")],
        ))

    def test_they_share_the_one_namespace(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="share one namespace"):
            self._with(
                constants=[Constant(name="Rate", data_type="Number",
                                    value=Value(number_value=1))],
                formulas=[Formula(name="Rate", data_type="Number",
                                  expression="1")],
            )

    def test_they_are_written_where_salesforce_writes_them(self):
        import xml.etree.ElementTree as ET
        from flowtool.xmlgen import METADATA_NS

        xml = generate(self._with(
            constants=[Constant(name="Rate", data_type="Number",
                                value=Value(number_value=20))],
            formulas=[Formula(name="Vat", data_type="Currency", expression="1")],
        ))
        tags = [c.tag.split("}")[-1] for c in ET.fromstring(xml)]
        assert tags.index("constants") < tags.index("formulas")
        assert tags.index("formulas") < tags.index("recordLookups")

    def test_an_expression_with_a_pipe_does_not_break_the_documentation(self):
        """A Markdown table cell ends at a pipe; a formula may contain one."""
        from flowtool.mermaid import to_markdown

        markdown = to_markdown(self._with(formulas=[
            Formula(name="Either", data_type="Boolean",
                    expression="{!a} || {!b}"),
        ]))
        row = next(line for line in markdown.splitlines() if "Either" in line)
        # An escaped pipe still contains the character, so count only the ones
        # that actually divide cells.
        dividers = row.count("|") - row.count("\|")
        assert dividers == 5, f"the expression split the row into cells: {row}"
        assert "\|\|" in row
