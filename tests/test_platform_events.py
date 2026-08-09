"""
Flows that run when a platform event is published — and the fossil that shares
their name.

The corpus called this `process_type:CustomEvent` and scored it `frees: 2`,
which made it look like the last real feature left. It is not the same thing.
The org says so outright: "Flows of type CustomEvent can't have the trigger type
PlatformEvent." A CustomEvent flow carries no trigger at all.

Both CustomEvent flows in the corpus are Process Builder migrations — variables
named `myVariable_event_context_collection`, an interviewLabel ending
`-6_InterviewLabel`, no `<start>` element — and the event they listen for
appears only inside `processMetadataValues`, Process Builder's own commentary,
which this parser treats as commentary and never writes back. Parsing one and
redeploying it would drop the record of which event starts it. That is a flow
that looks editable and loses something on the way out, so CustomEvent is out of
scope beside Workflow, and this file is about the thing people actually build.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Assignment,
    AssignmentItem,
    Flow,
    RecordCreate,
    FieldValue,
    Start,
    Value,
    Variable,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import OUT_OF_SCOPE, UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate

NOTE = Assignment(name="Note", label="Note", items=[
    AssignmentItem(to_reference="v_Text", value=Value(string_value="seen"))])
TEXT = Variable(name="v_Text", data_type="String")


def event_flow(**start_kwargs) -> Flow:
    fields = dict(next="Note", object="Order_Placed__e",
                  trigger_type="PlatformEvent")
    fields.update(start_kwargs)
    return Flow(api_name="On_Order", label="On Order", start=Start(**fields),
                elements=[NOTE], variables=[TEXT])


class TestTheTrigger:
    def test_it_survives_a_round_trip(self):
        before = event_flow()
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.model_dump() == before.model_dump()

    def test_the_xml_says_platform_event(self):
        xml = generate(event_flow())
        assert "<triggerType>PlatformEvent</triggerType>" in xml
        assert "<object>Order_Placed__e</object>" in xml

    def test_it_needs_an_event_to_listen_for(self):
        with pytest.raises(ValidationError, match="requires an object"):
            Start(next="Note", trigger_type="PlatformEvent")

    def test_it_takes_no_record_trigger_type(self):
        """
        An event is only ever published: there is no create-or-update to choose
        between. The org accepts one and ignores it, so this is the IR
        declining to ask a question the trigger does not pose.
        """
        with pytest.raises(ValidationError, match="only ever published"):
            Start(next="Note", object="Order_Placed__e",
                  trigger_type="PlatformEvent", record_trigger_type="Create")

    def test_an_ordinary_record_trigger_still_needs_one(self):
        with pytest.raises(ValidationError, match="record_trigger_type"):
            Start(next="Note", object="Account", trigger_type="RecordAfterSave")

    def test_entry_conditions_still_work(self):
        from flowtool.ir import RecordFilter

        flow = event_flow(filters=[RecordFilter(
            field="Amount__c", operator="GreaterThan",
            value=Value(number_value=100))])
        assert "<filterLogic>and</filterLogic>" in generate(flow)

    def test_the_event_is_read_as_the_triggering_record(self):
        """`$Record` is the event, exactly as it is the record elsewhere."""
        flow = Flow(
            api_name="On_Order", label="On Order",
            start=Start(next="Log", object="Order_Placed__e",
                        trigger_type="PlatformEvent"),
            elements=[RecordCreate(
                name="Log", label="Log", object="Task",
                fields=[FieldValue(field="Subject",
                                   value=Value(element_reference="$Record.Order_Id__c"))])],
        )
        assert "$Record.Order_Id__c" in generate(flow)


class TestReadingFromAnOrg:
    def org_xml(self, process_type="AutoLaunchedFlow", start_extra="") -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            f"<processType>{process_type}</processType><status>Draft</status>"
            "<assignments><name>Note</name><label>Note</label>"
            "<assignmentItems><assignToReference>v_Text</assignToReference>"
            "<operator>Assign</operator>"
            "<value><stringValue>seen</stringValue></value></assignmentItems>"
            "</assignments>"
            "<start><connector><targetReference>Note</targetReference></connector>"
            f"{start_extra}</start>"
            "<variables><name>v_Text</name><dataType>String</dataType>"
            "<isCollection>false</isCollection><isInput>false</isInput>"
            "<isOutput>false</isOutput></variables></Flow>"
        )

    def test_a_platform_event_flow_parses(self):
        flow = parse_flow(self.org_xml(start_extra=(
            "<object>Order_Placed__e</object>"
            "<triggerType>PlatformEvent</triggerType>")), api_name="X")
        assert flow.start.trigger_type == "PlatformEvent"
        assert flow.start.object == "Order_Placed__e"
        assert flow.start.record_trigger_type is None


class TestCustomEventIsNotThis:
    def test_it_is_refused(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>CustomEvent</processType><status>Draft</status>"
            "<assignments><name>Note</name><label>Note</label>"
            "<assignmentItems><assignToReference>v_Text</assignToReference>"
            "<operator>Assign</operator>"
            "<value><stringValue>seen</stringValue></value></assignmentItems>"
            "</assignments>"
            "<startElementReference>Note</startElementReference>"
            "<variables><name>v_Text</name><dataType>String</dataType>"
            "<isCollection>false</isCollection><isInput>false</isInput>"
            "<isOutput>false</isOutput></variables></Flow>"
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="X")
        assert "process_type:CustomEvent" in caught.value.codes

    def test_it_is_refused_by_decision_not_by_gap(self):
        """
        So a survey keeps counting it - the count is the evidence for the
        decision - and never recommends building it. Without this, the report
        named it the biggest available win.
        """
        assert "process_type:CustomEvent" in OUT_OF_SCOPE


class TestWhatTheUserSees:
    def test_the_trigger_reads_as_an_event(self):
        markdown = to_markdown(event_flow(), include_diagram=False)
        assert "every time one of these events is published" in markdown

    def test_the_start_node_does_not_say_none(self):
        """
        The usual caption is "Object Create (after save)". With no
        record_trigger_type that read "Order_Placed__e None (platform event)".
        """
        diagram = to_mermaid(event_flow())
        assert "Order_Placed__e published" in diagram
        assert "None" not in diagram
