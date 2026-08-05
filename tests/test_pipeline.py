"""
The pipeline's safety properties, driven with a scripted provider and scripted
keyboard input. No API key, no network, no org.

The property that matters most: nothing reaches the org that the user did not
approve, and a flow the model changes after approval is re-approved.
"""

import argparse
import asyncio
from typing import List

import pytest

import forge
from flowtool.llm import FlowGenerator
from flowtool.sfdc import ComponentProblem, DeployResult
from tests.test_llm import DANGLING, VALID, ScriptedProvider


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(org=None, out=None, deploy=False, no_validate=True,
                    model="x", effort="high", request=None, file=None,
                    activate=False, api_version="62.0")
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def no_link_lookup(monkeypatch):
    """The post-deploy link needs the org; these tests stub the org away."""
    async def fake(*_a, **_k):
        return "https://x/lightning/setup/Flows/home"

    monkeypatch.setattr(forge, "flow_builder_url", fake)


@pytest.fixture
def keyboard(monkeypatch):
    """Queue answers for input(); fail loudly if the code asks more than expected."""
    answers: List[str] = []

    def fake_input(prompt=""):
        if not answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        answer = answers.pop(0)
        print(f"{prompt}{answer}")
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return answers


class TestDesign:
    def test_approve_returns_the_flow(self, keyboard):
        keyboard.append("approve")
        generator = FlowGenerator(ScriptedProvider(VALID))
        result = forge.design(generator, "build it", _args())
        assert result is not None
        assert result.flow.api_name == "Won_Deal_Flow"

    def test_quit_returns_nothing(self, keyboard):
        keyboard.append("quit")
        generator = FlowGenerator(ScriptedProvider(VALID))
        assert forge.design(generator, "build it", _args()) is None

    def test_refine_loops_until_approved(self, keyboard):
        keyboard.extend(["refine", "also mark it hot", "approve"])
        provider = ScriptedProvider(VALID, VALID)
        result = forge.design(FlowGenerator(provider), "build it", _args())
        assert result is not None
        assert len(provider.calls) == 2, "refinement should have called the model again"
        assert provider.calls[1][-1].content == "also mark it hot"

    def test_answers_can_be_abbreviated(self, keyboard):
        keyboard.append("a")
        generator = FlowGenerator(ScriptedProvider(VALID))
        assert forge.design(generator, "build it", _args()) is not None


class TestApprovalGate:
    def test_quitting_at_the_graph_never_touches_the_org(self, keyboard, monkeypatch):
        keyboard.append("quit")

        async def must_not_run(*_a, **_k):
            raise AssertionError("contacted the org without approval")

        monkeypatch.setattr(forge, "validate_flow", must_not_run)
        code = asyncio.run(
            forge.run(_args(no_validate=False), ScriptedProvider(VALID), "build it")
        )
        assert code == 1

    def test_no_validate_never_touches_the_org(self, keyboard, monkeypatch):
        keyboard.append("approve")

        async def must_not_run(*_a, **_k):
            raise AssertionError("contacted the org despite --no-validate")

        monkeypatch.setattr(forge, "validate_flow", must_not_run)
        code = asyncio.run(forge.run(_args(), ScriptedProvider(VALID), "build it"))
        assert code == 0


class TestSalesforceRepairLoop:
    def _failing_then_passing(self, monkeypatch):
        """First checkOnly fails with a real-shaped error, the second passes."""
        calls = []

        async def fake_check(flow, instance_url, token, check_only=True):
            calls.append((flow.api_name, check_only))
            if len(calls) == 1:
                return DeployResult(
                    id="1", status="Failed", success=False,
                    failures=[ComponentProblem(
                        full_name="Won_Deal_Flow",
                        problem="You can't use the sObjectInputReference field with "
                                "the inputAssignments field.",
                        problem_type="Error",
                    )],
                )
            return DeployResult(id="2", status="Succeeded", success=True)

        monkeypatch.setattr(forge, "check", fake_check)
        monkeypatch.setattr(forge, "credentials", lambda *_a, **_k: ("https://x", "tok"))
        return calls

    def test_org_errors_are_repaired_and_re_approved(self, keyboard, monkeypatch):
        calls = self._failing_then_passing(monkeypatch)
        # approve -> validate fails -> yes, fix it -> approve the revision
        keyboard.extend(["approve", "yes", "approve"])

        provider = ScriptedProvider(VALID, VALID)
        code = asyncio.run(forge.run(_args(no_validate=False), provider, "build it"))

        assert code == 0
        assert len(calls) == 2, "should have re-validated after the repair"
        assert all(check_only for _, check_only in calls), "must never deploy here"

        # The model was told what Salesforce actually said.
        repair_instruction = provider.calls[1][-1].content
        assert "Salesforce rejected" in repair_instruction
        assert "sObjectInputReference" in repair_instruction

    def test_declining_the_repair_stops(self, keyboard, monkeypatch):
        self._failing_then_passing(monkeypatch)
        keyboard.extend(["approve", "no"])
        code = asyncio.run(
            forge.run(_args(no_validate=False), ScriptedProvider(VALID), "build it")
        )
        assert code == 1

    def test_quitting_the_re_approval_stops_before_deploy(self, keyboard, monkeypatch):
        calls = self._failing_then_passing(monkeypatch)
        keyboard.extend(["approve", "yes", "quit"])
        code = asyncio.run(
            forge.run(_args(no_validate=False, deploy=True),
                      ScriptedProvider(VALID, VALID), "build it")
        )
        assert code == 1
        assert all(check_only for _, check_only in calls), "deployed a rejected flow"


class TestDeployGate:
    def _always_passing(self, monkeypatch):
        calls = []

        async def fake_check(flow, instance_url, token, check_only=True):
            calls.append(check_only)
            return DeployResult(id="1", status="Succeeded", success=True)

        monkeypatch.setattr(forge, "check", fake_check)
        monkeypatch.setattr(forge, "credentials", lambda *_a, **_k: ("https://x", "tok"))
        return calls

    def test_deploy_requires_the_flag(self, keyboard, monkeypatch):
        calls = self._always_passing(monkeypatch)
        keyboard.append("approve")
        asyncio.run(forge.run(_args(no_validate=False), ScriptedProvider(VALID), "build it"))
        assert calls == [True], "validated only; no deploy without --deploy"

    def test_deploy_requires_a_second_yes(self, keyboard, monkeypatch):
        calls = self._always_passing(monkeypatch)
        keyboard.extend(["approve", "no"])
        asyncio.run(
            forge.run(_args(no_validate=False, deploy=True), ScriptedProvider(VALID), "build it")
        )
        assert calls == [True], "--deploy alone must not deploy"

    def test_confirmed_deploy_runs_for_real(self, keyboard, monkeypatch):
        calls = self._always_passing(monkeypatch)
        keyboard.extend(["approve", "yes"])
        code = asyncio.run(
            forge.run(_args(no_validate=False, deploy=True), ScriptedProvider(VALID), "build it")
        )
        assert code == 0
        assert calls == [True, False], "expected checkOnly then a real deploy"


ACTIVE = dict(VALID, status="Active", api_version="60.0")


class TestDeploymentPolicy:
    """
    Status decides whether a deploy starts running against live records. The
    model asked for Active unprompted once, so it is not the model's call.
    """

    def test_model_requested_active_is_overridden(self, keyboard):
        keyboard.append("approve")
        result = forge.design(FlowGenerator(ScriptedProvider(ACTIVE)), "x", _args())
        assert result.flow.status == "Draft"

    def test_activate_flag_opts_in(self, keyboard):
        keyboard.append("approve")
        result = forge.design(
            FlowGenerator(ScriptedProvider(VALID)), "x", _args(activate=True)
        )
        assert result.flow.status == "Active"

    def test_api_version_comes_from_the_tool(self, keyboard):
        keyboard.append("approve")
        result = forge.design(
            FlowGenerator(ScriptedProvider(ACTIVE)), "x", _args(api_version="67.0")
        )
        assert result.flow.api_version == "67.0"

    def test_the_user_is_told_which_status_they_are_approving(self, keyboard, capsys):
        keyboard.append("approve")
        forge.design(FlowGenerator(ScriptedProvider(VALID)), "x", _args(activate=True))
        assert "ACTIVE" in capsys.readouterr().out

    def test_a_repaired_flow_cannot_smuggle_active_back_in(self, keyboard, monkeypatch):
        """A repair round-trips through the model, so policy is re-applied."""
        calls = []

        async def fake_check(flow, instance_url, token, check_only=True):
            calls.append(flow.status)
            if len(calls) == 1:
                return DeployResult(id="1", status="Failed", success=False,
                                    failures=[ComponentProblem("f", "p", "Error")])
            return DeployResult(id="2", status="Succeeded", success=True)

        monkeypatch.setattr(forge, "check", fake_check)
        monkeypatch.setattr(forge, "credentials", lambda *_a, **_k: ("https://x", "tok"))
        keyboard.extend(["approve", "yes", "approve"])

        asyncio.run(
            forge.run(_args(no_validate=False), ScriptedProvider(VALID, ACTIVE), "x")
        )
        assert calls == ["Draft", "Draft"], calls


class TestArtifacts:
    def test_writes_xml_markdown_and_ir(self, keyboard, tmp_path):
        keyboard.append("approve")
        asyncio.run(
            forge.run(_args(out=str(tmp_path)), ScriptedProvider(VALID), "build it")
        )
        written = sorted(p.name for p in tmp_path.iterdir())
        assert written == [
            "Won_Deal_Flow.flow-meta.xml",
            "Won_Deal_Flow.ir.json",
            "Won_Deal_Flow.md",
        ]
        xml = (tmp_path / "Won_Deal_Flow.flow-meta.xml").read_text(encoding="utf-8")
        assert "<processType>AutoLaunchedFlow</processType>" in xml
