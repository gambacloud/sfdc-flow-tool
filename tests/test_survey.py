"""
The survey's job is to turn "what should we build next" into a number, so the
number has to be right: each blocker counted once per flow, every blocker
counted, and a flow that parses but loses something on the way back flagged
loudly rather than counted as covered.
"""

import json

import pytest

from flowtool.ir import FieldValue, Flow, GetRecords, RecordUpdate, Start, Value
from flowtool.xmlgen import generate
from survey import Survey, as_json, report

HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
    "<apiVersion>62.0</apiVersion><label>X</label>"
    "<processType>{pt}</processType><status>Draft</status>"
)
TAIL = "<start><connector><targetReference>E</targetReference></connector></start></Flow>"
LOOKUP = '<recordLookups><name>E</name><label>E</label><object>Account</object>{}</recordLookups>'


def xml(body: str = "", process_type: str = "AutoLaunchedFlow", lookup_extra: str = "") -> str:
    return HEAD.format(pt=process_type) + LOOKUP.format(lookup_extra) + body + TAIL


def clean_flow_xml() -> str:
    return generate(Flow(
        api_name="Good", label="Good",
        start=Start(next="Get"),
        elements=[
            GetRecords(name="Get", label="Get", object="Account", next="Mark"),
            RecordUpdate(name="Mark", label="Mark", object="Account",
                         fields=[FieldValue(field="Rating",
                                            value=Value(string_value="Hot"))]),
        ],
    ))


class TestCounting:
    def test_a_clean_flow_counts_as_parsed(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        assert survey.parsed == ["Good"]
        assert not survey.refused
        assert survey.total == 1

    def test_a_blocker_counts_once_per_flow_not_once_per_occurrence(self):
        # Three screens in one flow is one flow blocked by screens.
        survey = Survey()
        survey.add("Screeny", xml(
            "<screens><name>A</name></screens>"
            "<screens><name>B</name></screens>"
            "<screens><name>C</name></screens>"
        ))
        assert survey.codes["element:screens"] == 1

    def test_every_blocker_in_a_flow_is_counted(self):
        survey = Survey()
        survey.add("Both", xml(
            "<screens><name>A</name></screens><waits><name>W</name></waits>"
        ))
        assert survey.codes["element:screens"] == 1
        assert survey.codes["element:waits"] == 1
        assert survey.flows_by_code["element:waits"] == ["Both"]

    def test_counts_add_up_across_flows(self):
        survey = Survey()
        for name in ("A", "B", "C"):
            survey.add(name, xml("<screens><name>S</name></screens>"))
        survey.add("D", clean_flow_xml())
        assert survey.codes["element:screens"] == 3
        assert survey.total == 4
        assert len(survey.parsed) == 1

    def test_a_nested_blocker_is_counted_too(self):
        survey = Survey()
        survey.add("Picky", xml(lookup_extra="<queriedFields>Id</queriedFields>"))
        assert survey.codes["child:queriedFields"] == 1

    def test_a_screen_flow_is_counted_by_its_process_type(self):
        survey = Survey()
        survey.add("Screeny", xml(process_type="Flow"))
        assert survey.codes["process_type:Flow"] == 1

    def test_elements_are_tallied_only_for_flows_that_parse(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        survey.add("Bad", xml("<screens><name>S</name></screens>"))
        assert survey.element_counts["GetRecords"] == 1
        assert survey.element_counts["RecordUpdate"] == 1


class TestRoundTrip:
    def test_a_clean_flow_reports_no_round_trip_failure(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        assert survey.round_trip_failures == []

    def test_a_flow_that_loses_a_field_is_flagged(self, monkeypatch):
        """
        This is the finding that matters most: the flow parses, so it looks
        editable, but something would be dropped on the way back out.
        """
        import survey as survey_module

        real_generate = survey_module.generate

        def lossy(flow):
            # Simulate a compiler that forgets a field the parser can read.
            return real_generate(flow).replace(
                "<object>Account</object>", "<object>Contact</object>", 1
            )

        monkeypatch.setattr(survey_module, "generate", lossy)

        survey = Survey()
        survey.add("Good", clean_flow_xml())
        assert survey.parsed == ["Good"], "it still parses"
        assert survey.round_trip_failures, "but the loss must be reported"
        name, detail = survey.round_trip_failures[0]
        assert name == "Good"
        assert "Get" in detail or "changed" in detail


class TestRobustness:
    def test_unparseable_xml_is_a_finding_not_a_crash(self):
        survey = Survey()
        survey.add("Broken", "<Flow><unclosed>")
        assert "Broken" in survey.refused
        assert survey.codes["unparseable"] == 1

    def test_an_unexpected_error_is_recorded_rather_than_raised(self, monkeypatch):
        import survey as survey_module

        def boom(*_a, **_k):
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(survey_module, "parse_flow", boom)
        survey = Survey()
        survey.add("Odd", clean_flow_xml())
        assert survey.codes["parser_error"] == 1
        assert "something unforeseen" in survey.refused["Odd"][0]


class TestOutput:
    def test_the_report_survives_an_empty_survey(self, capsys):
        report(Survey(), verbose=False)
        assert "No flows found" in capsys.readouterr().out

    def test_the_report_names_the_biggest_win(self, capsys):
        survey = Survey()
        for name in ("A", "B"):
            survey.add(name, xml("<screens><name>S</name></screens>"))
        survey.add("C", xml("<waits><name>W</name></waits>"))
        report(survey, verbose=False)
        out = capsys.readouterr().out
        assert "3 flows" in out
        assert "Biggest single win" in out
        assert "element:screens" in out

    def test_json_is_serialisable_and_complete(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        survey.add("Bad", xml("<screens><name>S</name></screens>"))
        payload = json.loads(json.dumps(as_json(survey)))
        assert payload["total"] == 2
        assert payload["parsed"] == ["Good"]
        assert payload["codes"]["element:screens"] == 1
        assert payload["flows_by_code"]["element:screens"] == ["Bad"]
