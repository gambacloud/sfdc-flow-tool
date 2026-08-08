"""
Scheduled paths: the branches of a record-triggered flow that run later.

"Three days after it closes", "an hour before it is due", "chase it next week"
— the most-asked-for thing a flow does, and the first feature built this session
with no corpus evidence behind it. Ninety-one public flows contain not one
scheduledPaths element, because sample apps demonstrate screens and components,
not waiting. So the org is the only authority here and every shape below was put
through checkOnly before it was modelled.

What that probe found is the shape of this file. The org rejects a bad offset
unit, a record field that is not a date, a path on a before-save trigger, a
duplicate name, and a connector pointing at nothing. It *accepts*, and deploys:

    timeSource RecordField naming no record field
    offsetNumber with no offsetUnit
    offsetUnit with no offsetNumber

Those three are why the validators exist.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    FieldValue,
    Flow,
    RecordUpdate,
    ScheduledPath,
    Start,
    Value,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate


def after(**kwargs) -> ScheduledPath:
    fields = dict(name="Chase", next="Later", offset_number=3,
                  offset_unit="Days", time_source="RecordTriggerEvent")
    fields.update(kwargs)
    return ScheduledPath(**fields)


def flow(*paths, trigger="RecordAfterSave") -> Flow:
    return Flow(
        api_name="Chase_Flow", label="Chase Flow",
        start=Start(next="Mark", object="Opportunity",
                    record_trigger_type="CreateAndUpdate", trigger_type=trigger,
                    scheduled_paths=list(paths)),
        elements=[
            RecordUpdate(name="Mark", label="Mark", input_reference="$Record",
                         fields=[FieldValue(field="Rating",
                                            value=Value(string_value="Hot"))]),
            RecordUpdate(name="Later", label="Later", input_reference="$Record",
                         fields=[FieldValue(field="Rating",
                                            value=Value(string_value="Warm"))]),
        ],
    )


class TestRoundTrip:
    @pytest.mark.parametrize("path", [
        after(),
        after(label="Chase it"),
        after(offset_unit="Minutes", offset_number=30),
        after(offset_unit="Months", offset_number=1),
        after(offset_number=-1, time_source="RecordField",
              record_field="CloseDate"),
        after(max_batch_size=100),
        ScheduledPath(name="Async", next="Later", run_asynchronously=True),
        ScheduledPath(name="Ends", next=None, offset_number=1,
                      offset_unit="Days", time_source="RecordTriggerEvent"),
    ])
    def test_a_path_survives(self, path):
        before = flow(path)
        after_trip = parse_flow(generate(before), api_name=before.api_name)
        assert after_trip.model_dump() == before.model_dump()

    def test_three_paths_keep_their_order(self):
        before = flow(
            ScheduledPath(name="Async", next="Later", run_asynchronously=True),
            after(),
            after(name="Before_Close", offset_number=-1,
                  time_source="RecordField", record_field="CloseDate"),
        )
        returned = parse_flow(generate(before), api_name=before.api_name)
        assert [p.name for p in returned.start.scheduled_paths] == [
            "Async", "Chase", "Before_Close"
        ]

    def test_a_flow_with_no_paths_writes_none(self):
        before = flow()
        assert "scheduledPaths" not in generate(before)


class TestGeneratedXml:
    def test_the_path_carries_its_timing(self):
        xml = generate(flow(after()))
        assert "<offsetNumber>3</offsetNumber>" in xml
        assert "<offsetUnit>Days</offsetUnit>" in xml
        assert "<timeSource>RecordTriggerEvent</timeSource>" in xml

    def test_an_async_path_carries_only_its_type(self):
        """
        The org's own list of what may not accompany it: Label, TimeSource,
        OffsetUnit, OffsetNumber, RecordField, MaxBatchSize.
        """
        xml = generate(flow(ScheduledPath(name="Async", next="Later",
                                          run_asynchronously=True)))
        assert "<pathType>AsyncAfterCommit</pathType>" in xml
        for tag in ("offsetNumber", "offsetUnit", "timeSource", "recordField",
                    "maxBatchSize"):
            assert f"<{tag}>" not in xml

    def test_a_scheduled_path_carries_no_pathType(self):
        assert "pathType" not in generate(flow(after()))


class TestTheIrChecksWhatTheOrgDoesNot:
    def test_a_record_field_source_must_name_a_field(self):
        with pytest.raises(ValidationError, match="record_field must name one"):
            after(time_source="RecordField")

    def test_a_record_field_needs_that_source(self):
        with pytest.raises(ValidationError, match="only read when time_source"):
            after(record_field="CloseDate")

    def test_a_number_without_a_unit(self):
        with pytest.raises(ValidationError, match="does not say 3 of what"):
            ScheduledPath(name="P", next="Later", offset_number=3,
                          time_source="RecordTriggerEvent")

    def test_a_unit_without_a_number(self):
        with pytest.raises(ValidationError, match="does not say how many"):
            ScheduledPath(name="P", next="Later", offset_unit="Days",
                          time_source="RecordTriggerEvent")

    @pytest.mark.parametrize("extra", [
        {"label": "x"}, {"offset_number": 1, "offset_unit": "Days"},
        {"time_source": "RecordTriggerEvent"}, {"max_batch_size": 10},
    ])
    def test_an_async_path_carries_nothing_else(self, extra):
        with pytest.raises(ValidationError, match="AsyncAfterCommit"):
            ScheduledPath(name="P", next="Later", run_asynchronously=True,
                          **extra)

    def test_a_path_with_no_timing_at_all_is_allowed(self):
        """
        The org takes it and it has a reading - fire at the trigger. Refusing it
        would be guessing at intent rather than following a rule.
        """
        assert ScheduledPath(name="P", next="Later").offset_number is None


class TestWhereTheyAreAllowed:
    @pytest.mark.parametrize("trigger", ["RecordBeforeSave", "RecordBeforeDelete"])
    def test_not_on_a_before_trigger(self, trigger):
        """
        The org, one message per trigger type: "Flows with the trigger type
        RecordBeforeSave can't have scheduled paths." Nothing is committed yet,
        so there is no saved record to come back to.
        """
        with pytest.raises(ValidationError, match="can't have scheduled paths"):
            Start(next="Mark", object="Opportunity",
                  record_trigger_type="Update", trigger_type=trigger,
                  scheduled_paths=[after()])

    def test_not_on_an_autolaunched_flow(self):
        with pytest.raises(ValidationError, match="can't have scheduled paths"):
            Start(next="Mark", scheduled_paths=[after()])

    def test_two_paths_cannot_share_a_name(self):
        with pytest.raises(ValidationError, match="duplicate scheduled path"):
            Start(next="Mark", object="Opportunity",
                  record_trigger_type="CreateAndUpdate",
                  trigger_type="RecordAfterSave",
                  scheduled_paths=[after(), after()])

    def test_a_path_pointing_at_nothing_is_refused(self):
        with pytest.raises(ValidationError, match="scheduled path Chase"):
            flow(after(next="No_Such_Element"))


class TestReachability:
    """
    A scheduled path is an entry point, not a continuation. The flow ends after
    its immediate run and Salesforce starts it again later at the path's own
    connector - so everything a path reaches is reachable.
    """

    def test_what_only_a_path_reaches_is_not_an_orphan(self):
        assert flow(after()).warnings() == []

    def test_an_element_nothing_reaches_at_all_still_warns(self):
        f = flow(after())
        f.elements.append(
            RecordUpdate(name="Stranded", label="Stranded",
                         input_reference="$Record",
                         fields=[FieldValue(field="Rating",
                                            value=Value(string_value="Cold"))])
        )
        assert "Stranded" in "\n".join(Flow.model_validate(
            f.model_dump()).warnings())

    def test_the_connector_map_lists_the_paths(self):
        f = flow(after())
        assert "scheduled path Chase -> Later" in f.connector_map()


class TestWhatTheUserSees:
    @pytest.mark.parametrize("path,expected", [
        (after(), "3 days after the trigger"),
        (after(offset_number=1), "1 day after the trigger"),
        (after(offset_number=-1, time_source="RecordField",
               record_field="CloseDate"), "1 day before `CloseDate`"),
        (after(offset_number=30, offset_unit="Minutes"),
         "30 minutes after the trigger"),
        (after(offset_number=0, offset_unit="Hours"), "at the trigger"),
        (ScheduledPath(name="Async", next="Later", run_asynchronously=True),
         "right after saving, separately"),
    ])
    def test_the_timing_is_written_in_words(self, path, expected):
        """
        "-1 Days" is not how anyone says it, and the timing is the whole point
        of the branch. It is also the only place it appears on the diagram.
        """
        assert expected in to_markdown(flow(path), include_diagram=False)

    def test_the_diagram_shows_the_path_as_a_second_entry(self):
        diagram = to_mermaid(flow(after()))
        assert "START -.->|3 days after the trigger| Later" in diagram

    def test_the_immediate_branch_is_still_a_solid_line(self):
        assert "START --> Mark" in to_mermaid(flow(after()))


class TestReadingFromAnOrg:
    def org_xml(self, paths: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<recordUpdates><name>Later</name><label>Later</label>"
            "<inputReference>$Record</inputReference>"
            "<inputAssignments><field>Rating</field>"
            "<value><stringValue>Warm</stringValue></value></inputAssignments>"
            "</recordUpdates>"
            "<start><connector><targetReference>Later</targetReference></connector>"
            "<object>Opportunity</object>"
            "<recordTriggerType>CreateAndUpdate</recordTriggerType>"
            f"{paths}"
            "<triggerType>RecordAfterSave</triggerType></start></Flow>"
        )

    def test_a_path_from_an_org_parses(self):
        flow_ir = parse_flow(self.org_xml(
            "<scheduledPaths><name>Chase</name><label>Chase it</label>"
            "<connector><targetReference>Later</targetReference></connector>"
            "<offsetNumber>3</offsetNumber><offsetUnit>Days</offsetUnit>"
            "<timeSource>RecordTriggerEvent</timeSource></scheduledPaths>"
        ), api_name="X")
        path = flow_ir.start.scheduled_paths[0]
        assert (path.name, path.label, path.next) == ("Chase", "Chase it", "Later")
        assert (path.offset_number, path.offset_unit) == (3, "Days")

    def test_an_async_path_from_an_org_parses(self):
        flow_ir = parse_flow(self.org_xml(
            "<scheduledPaths><name>Async</name>"
            "<connector><targetReference>Later</targetReference></connector>"
            "<pathType>AsyncAfterCommit</pathType></scheduledPaths>"
        ), api_name="X")
        assert flow_ir.start.scheduled_paths[0].run_asynchronously is True

    def test_an_unknown_child_of_a_path_is_refused(self):
        """
        Same allowlist rule as everywhere else. A path attribute read as absent
        would be dropped on the next deploy, and the timing of a scheduled
        branch is not something to lose quietly.
        """
        from flowtool.parse import UnsupportedFlow

        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self.org_xml(
                "<scheduledPaths><name>Chase</name>"
                "<connector><targetReference>Later</targetReference></connector>"
                "<somethingNew>x</somethingNew></scheduledPaths>"
            ), api_name="X")
        assert "scheduled path Chase" in str(caught.value)
