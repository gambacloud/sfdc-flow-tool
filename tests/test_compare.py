"""Compare with the org: new vs existing per step, with what would change. Read-only, never blocking."""

import asyncio
from types import SimpleNamespace

from flowtool.compare import compare_steps
from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass
from flowtool.ir_object import CustomField, CustomObject
from flowtool.ir_prompt_template import PromptTemplate
from tests.test_llm import VALID as VALID_FLOW


class FakeOrg:
    """Answers the same calls OrgReader makes, from a dict."""

    def __init__(self, queries=None, describes=None, fail=None):
        self.queries = queries or {}
        self.describes = describes or {}
        self.fail = fail or set()

    async def query(self, soql, tooling=False):
        for needle, rows in self.queries.items():
            if needle in soql:
                if needle in self.fail:
                    raise RuntimeError("403: no access")
                return rows
        return []

    async def describe(self, sobject):
        return self.describes.get(sobject)


def step(name, value):
    return SimpleNamespace(step=SimpleNamespace(name=name), value=value)


def run(org, *steps, meta=None):
    return asyncio.run(compare_steps(org, list(steps), meta))


def apex(body="public with sharing class A {}"):
    return ApexClass(api_name="A", body=body)


def test_a_class_that_is_not_in_the_org_is_new():
    [r] = run(FakeOrg(), step("Apex", apex()))
    assert r["status"] == "new" and "class A" in r["summary"]


def test_an_identical_class_is_reported_as_matching():
    org = FakeOrg(queries={"FROM ApexClass": [{"Body": "public with sharing class A {}\r\n"}]})
    [r] = run(org, step("Apex", apex()))
    assert r["status"] == "exists" and "matches" in r["summary"] and r["diff"] == ""


def test_a_changed_class_comes_with_a_diff():
    org = FakeOrg(queries={"FROM ApexClass": [{"Body": "public with sharing class A {\n  Integer x = 1;\n}"}]})
    [r] = run(org, step("Apex", apex("public with sharing class A {\n  Integer x = 2;\n}")))
    assert r["status"] == "exists" and "replaces" in r["summary"]
    assert "-  Integer x = 1;" in r["diff"] and "+  Integer x = 2;" in r["diff"]


def test_a_flow_that_exists_says_which_version_it_would_add():
    flow = Flow.model_validate(VALID_FLOW)
    org = FakeOrg(queries={"FROM Flow ": [{"VersionNumber": 3, "Status": "Active"}]})
    [r] = run(org, step("Flow", flow))
    assert r["status"] == "exists" and "version 3" in r["summary"] and "version 4" in r["summary"]


def test_an_object_that_exists_reports_a_changed_label():
    obj = CustomObject(api_name="Invoice__c", label="Invoice", plural_label="Invoices")
    org = FakeOrg(describes={"Invoice__c": {"label": "Bill", "labelPlural": "Invoices", "fields": []}})
    [r] = run(org, step("Object", obj))
    assert r["status"] == "exists" and r["changes"] == ["label: 'Bill' -> 'Invoice'"]


def field(**kw):
    base = dict(api_name="Risk__c", object_api_name="Account", label="Risk", type="Picklist",
                picklist_values=["Low", "High"])
    return CustomField(**{**base, **kw})


def test_a_field_on_an_object_that_is_missing_is_new_with_the_reason():
    [r] = run(FakeOrg(), step("Field", field()))
    assert r["status"] == "new" and "not in the org yet" in r["summary"]


def test_a_new_field_on_an_existing_object_is_new():
    org = FakeOrg(describes={"Account": {"fields": [{"name": "Name"}]}})
    [r] = run(org, step("Field", field()))
    assert r["status"] == "new" and "Account.Risk__c" in r["summary"]


def test_an_existing_field_lists_what_would_change():
    existing = {"name": "Risk__c", "label": "Risk level", "type": "picklist",
                "picklistValues": [{"value": "Low", "active": True}, {"value": "Medium", "active": True}]}
    org = FakeOrg(describes={"Account": {"fields": [existing]}})
    [r] = run(org, step("Field", field()))
    assert r["status"] == "exists"
    assert "label: 'Risk level' -> 'Risk'" in r["changes"]
    assert "picklist values added: High" in r["changes"]
    assert "picklist values dropped: Medium" in r["changes"]


def test_an_unchanged_field_matches():
    existing = {"name": "Risk__c", "label": "Risk", "type": "picklist",
                "picklistValues": [{"value": "Low", "active": True}, {"value": "High", "active": True}]}
    org = FakeOrg(describes={"Account": {"fields": [existing]}})
    [r] = run(org, step("Field", field()))
    assert r["status"] == "exists" and "matches" in r["summary"]


def test_one_failed_look_up_is_unknown_and_does_not_hide_the_others():
    org = FakeOrg(queries={"FROM ApexClass": []}, fail={"FROM ApexClass"})
    org.queries = {"FROM ApexClass": [{"Body": "x"}]}
    org.fail = {"FROM ApexClass"}
    results = run(org, step("Apex", apex()), step("Object", CustomObject(api_name="Invoice__c", label="Invoice", plural_label="Invoices")))
    assert [r["status"] for r in results] == ["unknown", "new"]
    assert "Could not compare" in results[0]["summary"]


def test_results_keep_the_order_of_the_steps():
    results = run(FakeOrg(), step("one", apex()), step("two", apex()), step("three", apex()))
    assert [r["step"] for r in results] == ["one", "two", "three"]


def test_a_prompt_template_needs_a_metadata_connection():
    template = PromptTemplate(api_name="T", label="T", content="hi")
    [r] = run(FakeOrg(), step("Prompt", template), meta=None)
    assert r["status"] == "unknown"
