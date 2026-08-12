"""
The Custom Error element: deliberately rejects the record being saved.

A thrown failure, not a caught one - the opposite of a fault path. The org says
where it is allowed in one clean sentence:

    A flow can't include Custom Error elements when TriggerType is set to
    None.

So a screen flow and a plain, manually-invoked autolaunched flow both refuse
it; a record-triggered (or scheduled, or platform-event) flow accepts it. It
is also always terminal - giving it a connector, or a message with
is_field_error and no field_selection, both deploy as an opaque "An
unexpected error occurred" rather than a clean rejection, so both are refused
by the IR instead.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    CustomError,
    CustomErrorMessage,
    Flow,
    Start,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate


def message(**kwargs) -> CustomErrorMessage:
    fields = dict(error_message="Something is wrong")
    fields.update(kwargs)
    return CustomErrorMessage(**fields)


def reject(*messages, **kwargs) -> CustomError:
    fields = dict(name="Reject", label="Reject", messages=list(messages) or [message()])
    fields.update(kwargs)
    return CustomError(**fields)


def flow(error: CustomError, **kwargs) -> Flow:
    fields = dict(
        api_name="CustomError_Flow", label="Custom Error Flow",
        start=Start(next="Reject", object="Account", record_trigger_type="Update",
                    trigger_type="RecordBeforeSave"),
        variables=[],
        elements=[error],
    )
    fields.update(kwargs)
    return Flow(**fields)


def survives(f: Flow) -> bool:
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    return before == after


class TestRoundTrip:
    @pytest.mark.parametrize("error", [
        reject(message()),
        reject(message(field_selection="Name", is_field_error=True)),
        reject(message(), message(field_selection="Amount", is_field_error=True)),
        reject(message(field_selection="Name"), name="Reject"),
        reject(message(), description="Explains why this is here."),
    ])
    def test_a_custom_error_survives(self, error):
        assert survives(flow(error))

    @pytest.mark.parametrize("trigger_kwargs", [
        dict(next="Reject", object="Account", record_trigger_type="Update",
             trigger_type="RecordBeforeSave"),
        dict(next="Reject", object="Account", record_trigger_type="Update",
             trigger_type="RecordAfterSave"),
        dict(next="Reject", object="Account", record_trigger_type="Delete",
             trigger_type="RecordBeforeDelete"),
    ])
    def test_every_record_trigger_that_allows_it(self, trigger_kwargs):
        assert survives(flow(reject(message()), start=Start(**trigger_kwargs)))


class TestGeneratedXml:
    def test_no_connector_is_written(self):
        xml = generate(flow(reject(message())))
        errors = xml[xml.index("<customErrors>"):xml.index("</customErrors>")]
        assert "<connector>" not in errors

    def test_a_field_level_message(self):
        xml = generate(flow(reject(
            message(field_selection="Name", is_field_error=True))))
        assert "<fieldSelection>Name</fieldSelection>" in xml
        assert "<isFieldError>true</isFieldError>" in xml

    def test_a_general_message_omits_field_tags(self):
        xml = generate(flow(reject(message())))
        errors = xml[xml.index("<customErrors>"):xml.index("</customErrors>")]
        assert "<fieldSelection>" not in errors
        assert "<isFieldError>" not in errors


class TestTheIrChecksWhatTheOrgDoesNot:
    def test_a_connector_is_refused(self):
        with pytest.raises(ValidationError, match="terminal"):
            reject(message(), next="After")

    def test_a_field_error_needs_a_field(self):
        # Confirmed against a real dev org: this combination deploys as an
        # opaque "An unexpected error occurred" rather than a clean rejection.
        with pytest.raises(ValidationError, match="field_selection"):
            message(is_field_error=True)

    def test_at_least_one_message_is_required(self):
        with pytest.raises(ValidationError):
            CustomError(name="Reject", label="Reject", messages=[])


class TestWhereACustomErrorIsAllowed:
    def test_not_in_a_screen_flow(self):
        with pytest.raises(ValidationError, match="TriggerType"):
            flow(reject(message()), process_type="Flow",
                 start=Start(next="Reject"))

    def test_not_in_a_plain_autolaunched_flow(self):
        with pytest.raises(ValidationError, match="TriggerType"):
            flow(reject(message()), start=Start(next="Reject"))

    def test_not_on_a_scheduled_trigger(self):
        # Confirmed against a real dev org: refused with the exact same
        # message as a screen flow, even though Scheduled has a trigger_type.
        from flowtool.ir import Schedule

        with pytest.raises(ValidationError, match="TriggerType"):
            flow(reject(message()), start=Start(
                next="Reject", trigger_type="Scheduled",
                schedule=Schedule(start_date="2026-08-15",
                                  start_time="02:00:00.000Z", frequency="Daily"),
            ))


class TestParsing:
    def test_an_unknown_child_of_a_custom_error_message_is_refused(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<customErrors><name>Reject</name><label>Reject</label>"
            "<customErrorMessages><errorMessage>Bad</errorMessage>"
            "<somethingMadeUp>x</somethingMadeUp></customErrorMessages>"
            "</customErrors>"
            "<start><object>Account</object><recordTriggerType>Update</recordTriggerType>"
            "<triggerType>RecordBeforeSave</triggerType>"
            "<connector><targetReference>Reject</targetReference></connector></start>"
            "</Flow>"
        )
        with pytest.raises(UnsupportedFlow, match="somethingMadeUp"):
            parse_flow(xml)


class TestWhatTheUserSees:
    def test_the_diagram_names_the_message(self):
        assert "Something is wrong" in to_mermaid(flow(reject(message())))

    def test_the_markdown_flags_a_field_level_message(self):
        doc = to_markdown(flow(reject(
            message(field_selection="Name", is_field_error=True))))
        assert "`Name`" in doc
