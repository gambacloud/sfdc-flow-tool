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
        # Three waits in one flow is one flow blocked by waits.
        survey = Survey()
        survey.add("Screeny", xml(
            "<waits><name>A</name></waits>"
            "<waits><name>B</name></waits>"
            "<waits><name>C</name></waits>"
        ))
        assert survey.codes["element:waits"] == 1

    def test_every_blocker_in_a_flow_is_counted(self):
        survey = Survey()
        survey.add("Both", xml(
            "<formulas><name>F</name></formulas><waits><name>W</name></waits>"
        ))
        assert survey.codes["element:formulas"] == 1
        assert survey.codes["element:waits"] == 1
        assert survey.flows_by_code["element:waits"] == ["Both"]

    def test_counts_add_up_across_flows(self):
        survey = Survey()
        for name in ("A", "B", "C"):
            survey.add(name, xml("<waits><name>W</name></waits>"))
        survey.add("D", clean_flow_xml())
        assert survey.codes["element:waits"] == 3
        assert survey.total == 4
        assert len(survey.parsed) == 1

    def test_a_nested_blocker_is_counted_too(self):
        survey = Survey()
        survey.add("Picky", xml(lookup_extra="<limit>5</limit>"))
        assert survey.codes["child:limit"] == 1

    def test_an_unsupported_process_type_is_counted(self):
        survey = Survey()
        survey.add("Legacy", xml(process_type="Workflow"))
        assert survey.codes["process_type:Workflow"] == 1

    def test_elements_are_tallied_only_for_flows_that_parse(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        survey.add("Bad", xml("<waits><name>W</name></waits>"))
        assert survey.element_counts["GetRecords"] == 1
        assert survey.element_counts["RecordUpdate"] == 1


class TestWhatWouldActuallyHelp:
    """
    Counting how often a blocker appears answers the wrong question. A flow
    blocked by five things is freed by none of them individually, and the first
    real survey recommended a fix that would have unblocked nothing.
    """

    def test_a_code_that_never_stands_alone_frees_nothing(self):
        survey = Survey()
        survey.add("Two_Problems", xml(
            "<formulas><name>F</name></formulas><waits><name>W</name></waits>"
        ))
        assert survey.codes["element:formulas"] == 1, "it is still counted as seen"
        assert survey.would_unblock()["element:formulas"] == 0, "but it frees nothing"

    def test_a_sole_blocker_frees_its_flow(self):
        survey = Survey()
        survey.add("One_Problem", xml("<waits><name>W</name></waits>"))
        assert survey.would_unblock()["element:waits"] == 1

    def test_managed_flows_do_not_drive_the_recommendation(self):
        # The first real survey's top answer was a Salesforce-installed flow
        # nobody can edit.
        survey = Survey()
        survey.add("sfdc_default_Something", xml("<waits><name>W</name></waits>"))
        assert survey.codes["element:waits"] == 1
        assert survey.would_unblock()["element:waits"] == 0
        assert "sfdc_default_Something" in survey.managed

    @pytest.mark.parametrize("name,managed", [
        ("sfdc_default_ReportExport_Protection_Flow", True),
        ("acme__Vendor_Flow", True),
        ("Welcom_Potentials", False),
        ("My_Flow_2", False),
    ])
    def test_which_flows_count_as_managed(self, name, managed):
        from survey import is_managed

        assert is_managed(name) is managed

    def test_the_report_says_so_when_nothing_helps_alone(self, capsys):
        survey = Survey()
        survey.add("Two_Problems", xml(
            "<formulas><name>F</name></formulas><waits><name>W</name></waits>"
        ))
        report(survey, verbose=False)
        out = capsys.readouterr().out
        assert "No single addition unblocks anything" in out
        assert "Cheapest flows to reach" in out


class TestLegacyStart:
    def test_a_flow_with_no_start_element_still_parses(self):
        """
        Flows written before Flow Builder name their first element at the root
        and have no <start> at all. Three of the five flows in the first real
        survey were refused for this alone.
        """
        from flowtool.parse import parse_flow

        legacy = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>Old</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            + LOOKUP.format("")
            + "<startElementReference>E</startElementReference></Flow>"
        )
        flow = parse_flow(legacy, api_name="Old")
        assert flow.start.next == "E"

    def test_a_modern_start_connector_still_wins(self):
        from flowtool.parse import parse_flow

        both = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>Both</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            + LOOKUP.format("")
            + "<startElementReference>Stale</startElementReference>"
            "<start><connector><targetReference>E</targetReference></connector></start>"
            "</Flow>"
        )
        assert parse_flow(both, api_name="Both").start.next == "E"


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
            survey.add(name, xml("<formulas><name>F</name></formulas>"))
        survey.add("C", xml("<waits><name>W</name></waits>"))
        report(survey, verbose=False)
        out = capsys.readouterr().out
        assert "3 flows" in out
        assert "Biggest single win" in out
        assert "element:formulas" in out

    def test_json_is_serialisable_and_complete(self):
        survey = Survey()
        survey.add("Good", clean_flow_xml())
        survey.add("Bad", xml("<waits><name>W</name></waits>"))
        payload = json.loads(json.dumps(as_json(survey)))
        assert payload["total"] == 2
        assert payload["parsed"] == ["Good"]
        assert payload["codes"]["element:waits"] == 1
        assert payload["flows_by_code"]["element:waits"] == ["Bad"]


class TestOutOfScopeIsNeverRecommended:
    """
    Migrated Workflow Rules are refused by decision, not by gap. The report kept
    naming them as the biggest available win - the line people read first,
    saying the same thing every run, recommending work already ruled out.

    They stay counted: the count is the evidence for the decision.
    """

    def test_it_is_still_counted(self):
        survey = Survey()
        survey.add("Legacy", xml(process_type="Workflow"))
        assert survey.codes["process_type:Workflow"] == 1

    def test_it_never_frees_anything(self):
        survey = Survey()
        survey.add("Legacy", xml(process_type="Workflow"))
        assert survey.would_unblock()["process_type:Workflow"] == 0

    def test_a_real_gap_still_wins_the_recommendation(self, capsys):
        survey = Survey()
        # Two flows blocked only by a legacy process type, one by a real gap.
        for name in ("Legacy_A", "Legacy_B"):
            survey.add(name, xml(process_type="Workflow"))
        survey.add("Fixable", xml("<waits><name>W</name></waits>"))
        report(survey, verbose=False)
        out = capsys.readouterr().out
        assert "Biggest single win: supporting element:waits" in out, (
            "the recommendation must skip what was decided against, even when "
            "it appears in more flows"
        )

    def test_the_report_marks_it(self, capsys):
        survey = Survey()
        survey.add("Legacy", xml(process_type="Workflow"))
        report(survey, verbose=False)
        out = capsys.readouterr().out
        assert "out of scope" in out
        assert "counted, never recommended" in out

    def test_the_scope_decision_lives_with_the_refusals(self):
        """One list, next to the code that produces the codes it names."""
        from flowtool.parse import OUT_OF_SCOPE

        assert "process_type:Workflow" in OUT_OF_SCOPE
