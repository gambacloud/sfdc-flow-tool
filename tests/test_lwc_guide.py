from flowtool.ir_lwc import LightningComponent
from flowtool.lwc_guide import to_lwc_test_guide
from flowtool.planner import PlanStep, StepResult
from flowtool.report import render_html_fragment

JS = """
import { LightningElement, api } from 'lwc';
import { ShowToastEvent } from 'lightning/platformShowToastEvent';
import calculateAccountRisk from '@salesforce/apex/AccountRiskCalculator.calculateAccountRisk';
import SCORE from '@salesforce/schema/Account.Renewal_Risk_Score__c';
import NAME from '@salesforce/schema/Account.Name';
export default class AccountRiskCockpit extends LightningElement { @api recordId; }
"""
HTML = """<template>
  <lightning-card title="Risk">
    <lightning-button label="Run follow-up" onclick={run}></lightning-button>
  </lightning-card>
</template>"""


def _cockpit(**kw) -> LightningComponent:
    base = dict(
        api_name="accountRiskCockpit", js=JS, html=HTML,
        is_exposed=True, targets=["lightning__RecordPage"],
    )
    base.update(kw)
    return LightningComponent(**base)


def test_guide_lists_apex_fields_object_button_and_toast():
    guide = to_lwc_test_guide(_cockpit())
    assert "AccountRiskCalculator.calculateAccountRisk" in guide
    assert "Account.Renewal_Risk_Score__c" in guide
    assert "field-level security" in guide
    assert "Open a **Account** record" in guide
    assert "**Run follow-up**" in guide
    assert "toast" in guide


def test_only_custom_fields_are_called_out_as_dependencies():
    guide = to_lwc_test_guide(_cockpit())
    assert "`Account.Name`" not in guide


def test_unexposed_component_says_it_cannot_be_placed():
    guide = to_lwc_test_guide(_cockpit(is_exposed=False, targets=[]))
    assert "not exposed" in guide
    assert "<c-account-risk-cockpit>" in guide


def test_report_carries_the_lwc_guide():
    step = StepResult(
        step=PlanStep(artifact_type="lwc", name="Cockpit", brief="x"),
        value=_cockpit(), repairs=0, messages=[],
    )
    assert "How to test this component" in render_html_fragment([step])
