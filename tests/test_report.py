"""
flowtool/report.py - the rendering shared by mcp_server.py's report_html and
server.py's downloadable plan report. Pins down the two things that changed
when Flow steps grew a real diagram and per-element detail table: the
fragment carries both, and the standalone document only pays for inlining
mermaid.js when a Flow step is actually present.
"""

from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomObject
from flowtool.planner import PlanStep, StepResult
from flowtool.report import render_html_fragment, render_standalone_report

FLOW_IR = {
    "api_name": "Won_Deal_Flow",
    "label": "Won Deal Flow",
    "start": {
        "object": "Opportunity",
        "record_trigger_type": "CreateAndUpdate",
        "trigger_type": "RecordAfterSave",
        "next": "Get_Account",
    },
    "elements": [
        {
            "type": "GetRecords", "name": "Get_Account", "label": "Get Account",
            "object": "Account", "next": None,
        }
    ],
}


def _step(artifact_type: str, name: str, value) -> StepResult:
    return StepResult(
        step=PlanStep(artifact_type=artifact_type, name=name, brief="x"),
        value=value, repairs=0, messages=[],
    )


def _flow_step() -> StepResult:
    return _step("flow", "Flow", Flow(**FLOW_IR))


def _apex_step() -> StepResult:
    return _step(
        "apex", "Apex",
        ApexClass(api_name="Helper", body="public class Helper {}"),
    )


class TestRenderHtmlFragment:
    def test_flow_step_embeds_a_mermaid_diagram(self):
        fragment = render_html_fragment([_flow_step()])
        assert '<pre class="mermaid">' in fragment
        assert "Get_Account" in fragment  # the diagram source itself

    def test_flow_step_lists_per_element_detail(self):
        fragment = render_html_fragment([_flow_step()])
        # element_index's own table, not just a bare label list - what the
        # element actually does, matching the in-app Documentation tab.
        assert "Get Account" in fragment
        assert "<table>" in fragment

    def test_non_flow_steps_are_unaffected(self):
        fragment = render_html_fragment([_apex_step()])
        assert '<pre class="mermaid">' not in fragment
        assert "public class Helper" in fragment


class TestRenderStandaloneReport:
    def test_inlines_mermaid_js_only_when_a_flow_step_is_present(self):
        with_flow = render_standalone_report([_flow_step()], title="t")
        without_flow = render_standalone_report([_apex_step()], title="t")

        assert "mermaid.initialize" in with_flow
        assert "mermaid.initialize" not in without_flow

    def test_document_is_self_contained(self):
        report = render_standalone_report([_flow_step()], title="Plan report")
        assert report.startswith("<!doctype html>")
        assert "<title>Plan report</title>" in report

    def test_flow_report_carries_its_own_csp_allowing_inline_script(self):
        # The live app's own CSP is script-src 'self' - fine for the app,
        # but a downloaded, self-contained file has no server left to send
        # that header once it's reopened from disk, so this document must
        # govern itself or its inlined mermaid.js silently fails to run.
        report = render_standalone_report([_flow_step()], title="t")
        assert 'http-equiv="Content-Security-Policy"' in report
        assert "unsafe-inline" in report

    def test_non_flow_report_has_no_csp_meta(self):
        # No inlined script, nothing for a CSP meta tag to permit.
        report = render_standalone_report([_apex_step()], title="t")
        assert "Content-Security-Policy" not in report
