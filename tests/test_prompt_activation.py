"""Activating a deployed Prompt Template: read the org's version id, deploy it back as the active one."""

import asyncio
import io
import zipfile
from types import SimpleNamespace

import pytest

import server
from flowtool.ir_prompt_template import PromptTemplate
from flowtool.planner import PlanStep, StepResult
from flowtool.prompt_activation import activate_templates, with_active_version
from flowtool.sfdc import ComponentProblem, DeployResult

RETRIEVED = """<?xml version="1.0" encoding="UTF-8"?>
<GenAiPromptTemplate xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>Audits</description>
    <developerName>QA_Audit</developerName>
    <templateVersions>
        <inputs><description>the text</description><apiName>t</apiName></inputs>
        <status>Published</status>
        <versionIdentifier>abc123=_1</versionIdentifier>
    </templateVersions>
    <type>einstein_gpt__flex</type>
</GenAiPromptTemplate>
"""


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


def test_the_newest_version_becomes_the_active_one():
    out = with_active_version(RETRIEVED)
    assert "<activeVersionIdentifier>abc123=_1</activeVersionIdentifier>" in out
    assert out.count("<activeVersionIdentifier>") == 1
    # a direct child of the template, not of an input that also has a <description>
    assert out.index("<activeVersionIdentifier>") < out.index("<templateVersions>")


def test_nothing_to_do_when_already_active_or_never_versioned():
    assert with_active_version(with_active_version(RETRIEVED)) is None
    assert with_active_version("<GenAiPromptTemplate><developerName>X</developerName></GenAiPromptTemplate>") is None


class FakeClient:
    def __init__(self, retrieved, ok=True):
        self.retrieved = retrieved
        self.ok = ok
        self.deployed = []

    async def start_retrieve_types(self, types):
        self.types = types
        return "job"

    async def wait_for_retrieve(self, job):
        return zip_of({f"genAiPromptTemplates/{n}.genAiPromptTemplate": x for n, x in self.retrieved.items()})

    async def deploy_and_wait(self, zip_bytes, check_only=True):
        self.deployed.append((zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist(), check_only))
        if self.ok:
            return DeployResult("1", "Succeeded", True)
        return DeployResult("1", "Failed", False, [ComponentProblem("QA_Audit", "nope", "Error")])


def run(coro):
    return asyncio.run(coro)


def test_activation_redeploys_the_retrieved_file_for_real():
    client = FakeClient({"QA_Audit": RETRIEVED})
    assert run(activate_templates(client, ["QA_Audit"], "68.0")) == ["QA_Audit"]
    names, check_only = client.deployed[0]
    assert "genAiPromptTemplates/QA_Audit.genAiPromptTemplate" in names and check_only is False


def test_an_already_active_template_is_left_alone():
    client = FakeClient({"QA_Audit": with_active_version(RETRIEVED)})
    assert run(activate_templates(client, ["QA_Audit"], "68.0")) == []
    assert client.deployed == []


def test_a_template_that_cannot_be_read_back_is_an_error():
    with pytest.raises(RuntimeError, match="could not read back"):
        run(activate_templates(FakeClient({}), ["QA_Audit"], "68.0"))


def test_a_rejected_activation_surfaces_the_orgs_words():
    with pytest.raises(RuntimeError, match="nope"):
        run(activate_templates(FakeClient({"QA_Audit": RETRIEVED}, ok=False), ["QA_Audit"], "68.0"))


# --- wired into the plan deploy ----------------------------------------------

def session_with_template():
    template = PromptTemplate(api_name="QA_Audit", label="QA", content="x {!$Input:t}",
                              inputs=[{"name": "t"}])
    step = StepResult(step=PlanStep(artifact_type="prompt_template", name="P", brief="x"),
                      value=template, repairs=0, messages=[])
    return SimpleNamespace(steps=[step], reference=None, api_version="62.0", permission_set_config=None,
                           plan=SimpleNamespace(agent_user=None))


@pytest.fixture
def patched(monkeypatch):
    calls = {"activated": None}

    async def fake_validate(url, token, files, types, api_version="62.0", check_only=True):
        calls["check_only"] = check_only
        return DeployResult("1", "Succeeded", True)

    async def fake_activate(client, names, api_version):
        calls["activated"] = names
        if calls.get("fail"):
            raise RuntimeError("the org said no")
        return names

    class FakeMeta:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(server, "validate_bundle", fake_validate)
    monkeypatch.setattr(server, "activate_templates", fake_activate)
    monkeypatch.setattr(server, "MetadataClient", FakeMeta)
    return calls


def test_a_real_deploy_activates_the_templates(patched):
    result = run(server._deploy_plan_bundle(session_with_template(), "u", "t", check_only=False))
    assert result.success and patched["activated"] == ["QA_Audit"]


def test_a_validate_only_run_activates_nothing(patched):
    run(server._deploy_plan_bundle(session_with_template(), "u", "t", check_only=True))
    assert patched["activated"] is None


def test_a_failed_activation_makes_the_deploy_report_failure(patched):
    patched["fail"] = True
    result = run(server._deploy_plan_bundle(session_with_template(), "u", "t", check_only=False))
    assert not result.success and "could not be activated" in str(result.failures[-1])
