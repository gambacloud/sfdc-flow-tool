"""
The Pause element: the flow stops, and Salesforce resumes it later.

The other half of "do this later", and the surprise is where it is allowed. The
org says it twice:

    Flows of type "Screen Flow" can't include Pause elements.
    A flow can't include Pause elements when TriggerType is set to
    Record-Run After Save.

Between them that leaves exactly one kind of flow that may pause — a plain
autolaunched one — which is the precise mirror of a scheduled path, allowed only
where a Pause is not. The two are the same request in the two forms Salesforce
provides, and which one is right is decided by what started the flow. The first
probe of this element wasted thirteen cases learning that, by putting the Pause
in a record-triggered flow.

Inside a wait the org checks nothing at all. It accepted an AlarmEvent with no
parameters, one whose parameter was named AlarmTimeX, and an eventType of
BananaEvent — all three deploy, and a pause with no time to resume at simply
never resumes. Everything in TestTheIrChecksWhatTheOrgDoesNot is the only thing
standing between those and a flow that quietly stops forever.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Assignment,
    AssignmentItem,
    Condition,
    Flow,
    InputAssignment,
    Start,
    Value,
    Variable,
    Wait,
    WaitEvent,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate

AFTER = Assignment(name="After", label="After", items=[
    AssignmentItem(to_reference="marker", value=Value(string_value="done"))])


def alarm(**kwargs) -> WaitEvent:
    fields = dict(
        name="Resume", label="When due", next="After",
        input_parameters=[InputAssignment(
            name="AlarmTime", value=Value(element_reference="whenToResume"))])
    fields.update(kwargs)
    return WaitEvent(**fields)


def date_ref(**kwargs) -> WaitEvent:
    fields = dict(
        name="From_Close", label="3 days from close",
        event_type="DateRefAlarmEvent", next="After",
        input_parameters=[
            InputAssignment(name="SalesforceObject",
                            value=Value(string_value="Opportunity")),
            InputAssignment(name="BaseDateTimeFieldName",
                            value=Value(string_value="CloseDate")),
            InputAssignment(name="RecordId",
                            value=Value(element_reference="theId")),
            InputAssignment(name="TimeOffset", value=Value(number_value=3)),
            InputAssignment(name="TimeOffsetUnit",
                            value=Value(string_value="Days")),
        ])
    fields.update(kwargs)
    return WaitEvent(**fields)


def hold(*events, **kwargs) -> Wait:
    fields = dict(name="Hold", label="Hold", wait_events=list(events),
                  default_next="After")
    fields.update(kwargs)
    return Wait(**fields)


def flow(wait: Wait, **kwargs) -> Flow:
    fields = dict(
        api_name="Wait_Flow", label="Wait Flow", start=Start(next="Hold"),
        variables=[Variable(name="whenToResume", data_type="DateTime"),
                   Variable(name="marker", data_type="String"),
                   Variable(name="theId", data_type="String")],
        elements=[wait, AFTER],
    )
    fields.update(kwargs)
    return Flow(**fields)


def survives(f: Flow) -> bool:
    """By element name, not list order: the XML groups elements by tag."""
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    if {k: v for k, v in before.items() if k != "elements"} != {
            k: v for k, v in after.items() if k != "elements"}:
        return False
    return ({e["name"]: e for e in before["elements"]}
            == {e["name"]: e for e in after["elements"]})


class TestRoundTrip:
    @pytest.mark.parametrize("wait", [
        hold(alarm()),
        hold(date_ref()),
        hold(alarm(), date_ref()),
        hold(alarm(conditions=[Condition(left="marker", operator="EqualTo",
                                         right=Value(string_value="go"))])),
        hold(alarm(), default_next=None),
        hold(alarm(next=None)),
        hold(alarm(), fault_next="After"),
        hold(alarm(label=None)),
        hold(WaitEvent(name="On_Event", label="When it arrives",
                       event_type="My_Event__e", next="After")),
    ])
    def test_a_pause_survives(self, wait):
        assert survives(flow(wait))

    def test_a_platform_event_keeps_its_own_type(self):
        """
        The parameter bag exists for this: the IR knows nothing about
        My_Event__e and must still carry it through unchanged.
        """
        returned = parse_flow(generate(flow(hold(WaitEvent(
            name="On_Event", event_type="My_Event__e", next="After")))),
            api_name="Wait_Flow")
        pause = next(e for e in returned.elements if isinstance(e, Wait))
        assert pause.wait_events[0].event_type == "My_Event__e"


class TestGeneratedXml:
    def test_the_default_label_is_always_written(self):
        """
        Required by the org even with no default connector to label:
        "Required field is missing: defaultConnectorLabel".
        """
        xml = generate(flow(hold(alarm(), default_next=None)))
        assert "<defaultConnectorLabel>" in xml
        assert "<defaultConnector>" not in xml

    def test_a_pause_writes_no_plain_connector(self):
        xml = generate(flow(hold(alarm())))
        waits = xml[xml.index("<waits>"):xml.index("</waits>")]
        assert "<connector>" in waits, "the event has one"
        assert "<defaultConnector>" in waits
        # The element itself has no <connector> of its own, only the event's.
        assert waits.count("<connector>") == 1

    def test_the_parameters_are_written_as_given(self):
        xml = generate(flow(hold(alarm())))
        assert "<name>AlarmTime</name>" in xml
        assert "<elementReference>whenToResume</elementReference>" in xml


class TestTheIrChecksWhatTheOrgDoesNot:
    def test_an_alarm_needs_a_time(self):
        with pytest.raises(ValidationError, match="AlarmTime"):
            WaitEvent(name="R", next="After")

    def test_a_misspelled_parameter_is_caught(self):
        """
        The org took AlarmTimeX without a word. The flow deploys and never
        resumes.
        """
        with pytest.raises(ValidationError, match="AlarmTime"):
            WaitEvent(name="R", next="After", input_parameters=[
                InputAssignment(name="AlarmTimeX",
                                value=Value(element_reference="whenToResume"))])

    def test_a_date_reference_needs_a_record(self):
        with pytest.raises(ValidationError, match="RecordId"):
            WaitEvent(name="R", next="After", event_type="DateRefAlarmEvent",
                      input_parameters=[InputAssignment(
                          name="SalesforceObject",
                          value=Value(string_value="Opportunity"))])

    def test_an_offset_needs_its_unit(self):
        with pytest.raises(ValidationError, match="TimeOffsetUnit"):
            date_ref(input_parameters=[
                InputAssignment(name="SalesforceObject",
                                value=Value(string_value="Opportunity")),
                InputAssignment(name="BaseDateTimeFieldName",
                                value=Value(string_value="CloseDate")),
                InputAssignment(name="RecordId",
                                value=Value(element_reference="theId")),
                InputAssignment(name="TimeOffset", value=Value(number_value=3)),
            ])

    def test_a_date_reference_with_no_offset_is_allowed(self):
        """Resume on the date itself. Both or neither, as on a scheduled path."""
        event = date_ref(input_parameters=[
            InputAssignment(name="SalesforceObject",
                            value=Value(string_value="Opportunity")),
            InputAssignment(name="BaseDateTimeFieldName",
                            value=Value(string_value="CloseDate")),
            InputAssignment(name="RecordId",
                            value=Value(element_reference="theId")),
        ])
        assert event.event_type == "DateRefAlarmEvent"

    def test_an_unknown_event_type_is_not_second_guessed(self):
        """
        A platform event's API name goes in the same field, so there is no enum
        to check against and no parameters this IR could know the names of.
        """
        assert WaitEvent(name="R", event_type="Anything__e").input_parameters == []

    def test_a_pause_has_no_plain_next(self):
        with pytest.raises(ValidationError, match="does not have a plain"):
            Wait(name="Hold", label="Hold", next="After")

    def test_two_events_cannot_share_a_name(self):
        with pytest.raises(ValidationError, match="duplicate wait event"):
            hold(alarm(), alarm())

    def test_an_event_pointing_at_nothing_is_refused(self):
        with pytest.raises(ValidationError, match="Hold.Resume.next"):
            flow(hold(alarm(next="No_Such_Element")))


class TestWhereAPauseIsAllowed:
    def test_not_in_a_screen_flow(self):
        with pytest.raises(ValidationError, match="Screen Flow"):
            flow(hold(alarm()), process_type="Flow")

    def test_not_in_a_record_triggered_flow(self):
        with pytest.raises(ValidationError, match="TriggerType"):
            flow(hold(alarm()), start=Start(
                next="Hold", object="Opportunity",
                record_trigger_type="CreateAndUpdate",
                trigger_type="RecordAfterSave"))

    def test_the_error_points_at_scheduled_paths_instead(self):
        """
        The request behind both is the same one. Someone who reached for a
        Pause in a record-triggered flow wants a scheduled path and has no
        reason to know that.
        """
        with pytest.raises(ValidationError, match="scheduled_path"):
            flow(hold(alarm()), start=Start(
                next="Hold", object="Opportunity",
                record_trigger_type="CreateAndUpdate",
                trigger_type="RecordAfterSave"))

    def test_an_autolaunched_flow_is_fine(self):
        assert flow(hold(alarm())).elements


class TestReachability:
    def test_what_a_wait_event_reaches_is_not_an_orphan(self):
        assert flow(hold(alarm())).warnings() == []

    def test_what_only_the_default_reaches_is_not_an_orphan(self):
        assert flow(hold(alarm(next=None))).warnings() == []

    def test_the_connector_map_lists_every_exit(self):
        text = flow(hold(alarm(), date_ref())).connector_map()
        assert "Hold on Resume -> After" in text
        assert "Hold on From_Close -> After" in text
        assert "Hold default -> After" in text


class TestWhatTheUserSees:
    def test_the_document_says_what_it_waits_for(self):
        markdown = to_markdown(flow(hold(alarm())), include_diagram=False)
        assert "waits until whenToResume" in markdown

    def test_a_date_reference_reads_as_a_duration(self):
        markdown = to_markdown(flow(hold(date_ref())), include_diagram=False)
        assert "3 days from CloseDate" in markdown

    def test_the_next_column_points_at_the_events(self):
        """
        A Pause has no plain `next`, so the column would otherwise say "End"
        for an element that continues.
        """
        assert "see events" in to_markdown(flow(hold(alarm())),
                                           include_diagram=False)

    def test_every_exit_is_dotted_on_the_diagram(self):
        """Time passes on each of these edges. The flow is not running."""
        diagram = to_mermaid(flow(hold(alarm())))
        assert 'Hold -.->|"When due"| After' in diagram
        assert 'Hold -.->|"Anything else"| After' in diagram

    def test_an_unknown_event_type_still_says_something(self):
        markdown = to_markdown(
            flow(hold(WaitEvent(name="On_Event", event_type="My_Event__e",
                                next="After"))),
            include_diagram=False)
        assert "My_Event__e" in markdown


class TestReadingFromAnOrg:
    def org_xml(self, waits: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<assignments><name>After</name><label>After</label>"
            "<assignmentItems><assignToReference>marker</assignToReference>"
            "<operator>Assign</operator>"
            "<value><stringValue>done</stringValue></value></assignmentItems>"
            "</assignments>"
            f"{waits}"
            "<start><connector><targetReference>Hold</targetReference></connector>"
            "</start>"
            "<variables><name>whenToResume</name><dataType>DateTime</dataType>"
            "<isCollection>false</isCollection><isInput>false</isInput>"
            "<isOutput>false</isOutput></variables></Flow>"
        )

    WAIT = (
        "<waits><name>Hold</name><label>Hold</label>"
        "<defaultConnector><targetReference>After</targetReference>"
        "</defaultConnector>"
        "<defaultConnectorLabel>Anything else</defaultConnectorLabel>"
        "<waitEvents><name>Resume</name><label>When due</label>"
        "<connector><targetReference>After</targetReference></connector>"
        "<eventType>AlarmEvent</eventType>"
        "<inputParameters><name>AlarmTime</name>"
        "<value><elementReference>whenToResume</elementReference></value>"
        "</inputParameters></waitEvents></waits>"
    )

    def test_a_pause_from_an_org_parses(self):
        flow_ir = parse_flow(self.org_xml(self.WAIT), api_name="X")
        pause = next(e for e in flow_ir.elements if isinstance(e, Wait))
        assert pause.default_next == "After"
        assert pause.default_label == "Anything else"
        assert pause.wait_events[0].name == "Resume"

    def test_an_unknown_child_of_a_wait_event_is_refused(self):
        from flowtool.parse import UnsupportedFlow

        with pytest.raises(UnsupportedFlow):
            parse_flow(
                self.org_xml(self.WAIT.replace(
                    "</waitEvents>", "<somethingNew>x</somethingNew></waitEvents>"
                )),
                api_name="X",
            )
