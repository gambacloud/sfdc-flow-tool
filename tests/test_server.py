"""
The HTTP surface, with the approval gate tested server-side.

A gate that lives only in the browser is not a gate: anyone can call the
endpoint directly. These assert the server refuses on its own.
"""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.llm import FlowGenerator
from flowtool.sfdc import ComponentProblem, DeployResult
from tests.test_llm import VALID, ScriptedProvider

ACTIVE = dict(VALID, status="Active", api_version="60.0")


@pytest.fixture
def client(monkeypatch):
    server.SESSIONS.clear()
    monkeypatch.setattr(server, "credentials", lambda org: ("https://x", "tok"))
    return TestClient(server.app)


@pytest.fixture
def scripted(monkeypatch):
    """Make /api/design use a scripted model instead of a real provider."""
    def install(*payloads):
        provider = ScriptedProvider(*payloads)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


def design(client, **body):
    response = client.post("/api/design", json={"request": "build it", **body})
    assert response.status_code == 200, response.text
    return response.json()


def stub_validate(monkeypatch, *results):
    """Queue DeployResults for successive validate/deploy calls."""
    queue = list(results)
    calls = []

    async def fake(instance_url, token, name, xml, api_version="62.0", check_only=True):
        calls.append(check_only)
        return queue.pop(0) if queue else DeployResult("1", "Succeeded", True)

    monkeypatch.setattr(server, "validate_flow", fake)
    return calls


OK = DeployResult("1", "Succeeded", True)
FAILED = DeployResult(
    "2", "Failed", False,
    failures=[ComponentProblem("Won_Deal_Flow", "nothing is connected to Start", "Error")],
)


class TestDesign:
    def test_returns_a_diagram_and_the_artifacts(self, client, scripted):
        scripted(VALID)
        data = design(client)
        assert data["version"] == 1
        assert data["approved"] is False
        assert "flowchart TD" in data["mermaid"]
        assert data["ir"]["api_name"] == "Won_Deal_Flow"

    def test_empty_request_is_rejected(self, client, scripted):
        scripted(VALID)
        assert client.post("/api/design", json={"request": "  "}).status_code == 400

    def test_artifacts_are_downloadable(self, client, scripted):
        scripted(VALID)
        session_id = design(client)["session_id"]
        xml = client.get(f"/api/session/{session_id}/xml")
        assert xml.status_code == 200
        assert "<processType>AutoLaunchedFlow</processType>" in xml.text
        assert client.get(f"/api/session/{session_id}/markdown").status_code == 200
        assert client.get(f"/api/session/{session_id}/nope").status_code == 404


class TestApprovalGate:
    def test_validate_refuses_without_approval(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]

        response = client.post("/api/validate", json={"session_id": session_id})
        assert response.status_code == 403
        assert not calls, "contacted the org without approval"

    def test_deploy_refuses_without_approval(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]

        response = client.post(
            "/api/deploy", json={"session_id": session_id, "confirm": True}
        )
        assert response.status_code == 403
        assert not calls

    def test_deploy_refuses_without_confirmation(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        response = client.post("/api/deploy", json={"session_id": session_id})
        assert response.status_code == 400
        assert not calls

    def test_approving_a_stale_version_is_rejected(self, client, scripted):
        provider = scripted(VALID, VALID)
        session_id = design(client)["session_id"]
        client.post(
            "/api/refine", json={"session_id": session_id, "instruction": "change it"}
        )

        # v1 is what the browser last rendered; the flow is now v2.
        response = client.post(
            "/api/approve", json={"session_id": session_id, "version": 1}
        )
        assert response.status_code == 409
        assert "changed" in response.json()["detail"]

    def test_a_refinement_revokes_approval(self, client, scripted, monkeypatch):
        scripted(VALID, VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        refined = client.post(
            "/api/refine", json={"session_id": session_id, "instruction": "change it"}
        ).json()
        assert refined["approved"] is False

        assert client.post(
            "/api/validate", json={"session_id": session_id}
        ).status_code == 403
        assert not calls

    def test_unknown_session_is_a_404(self, client):
        assert client.post("/api/validate", json={"session_id": "nope"}).status_code == 404


class TestRepairLoop:
    def test_failures_come_back_and_can_be_repaired(self, client, scripted, monkeypatch):
        provider = scripted(VALID, VALID)
        stub_validate(monkeypatch, FAILED)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        result = client.post("/api/validate", json={"session_id": session_id}).json()
        assert result["success"] is False
        assert "nothing is connected to Start" in result["failures"][0]

        repaired = client.post("/api/repair", json={"session_id": session_id}).json()
        assert repaired["version"] == 2
        assert repaired["approved"] is False, "a repaired flow needs re-approval"

        # The model was told what Salesforce actually said.
        assert "nothing is connected to Start" in provider.calls[1][-1].content

    def test_repair_without_a_validation_is_rejected(self, client, scripted):
        scripted(VALID)
        session_id = design(client)["session_id"]
        assert client.post(
            "/api/repair", json={"session_id": session_id}
        ).status_code == 400


class TestDeploymentPolicy:
    def test_model_requested_active_is_overridden(self, client, scripted):
        scripted(ACTIVE)
        assert design(client)["status"] == "Draft"

    def test_activate_opts_in(self, client, scripted):
        scripted(VALID)
        assert design(client, activate=True)["status"] == "Active"

    def test_api_version_comes_from_the_request(self, client, scripted):
        scripted(ACTIVE)
        assert design(client, api_version="67.0")["api_version"] == "67.0"

    def test_a_repair_cannot_smuggle_active_back_in(self, client, scripted, monkeypatch):
        scripted(VALID, ACTIVE)
        stub_validate(monkeypatch, FAILED)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})
        client.post("/api/validate", json={"session_id": session_id})

        repaired = client.post("/api/repair", json={"session_id": session_id}).json()
        assert repaired["status"] == "Draft"


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>60.0</apiVersion>
    <recordUpdates>
        <name>Mark_Hot</name><label>Mark hot</label>
        <locationX>176</locationX><locationY>150</locationY>
        <inputAssignments><field>Rating</field>
            <value><stringValue>Hot</stringValue></value></inputAssignments>
        <object>Account</object>
    </recordUpdates>
    <label>Existing Flow</label>
    <processType>AutoLaunchedFlow</processType>
    <start>
        <locationX>176</locationX><locationY>0</locationY>
        <connector><targetReference>Mark_Hot</targetReference></connector>
        <object>Opportunity</object>
        <recordTriggerType>Update</recordTriggerType>
        <triggerType>RecordAfterSave</triggerType>
    </start>
    <status>Active</status>
</Flow>"""

SCREEN_XML = SAMPLE_XML.replace(
    "<label>Existing Flow</label>",
    "<screens><name>Ask</name></screens><label>Existing Flow</label>",
)


def stub_org(monkeypatch, xml=SAMPLE_XML, flows=None):
    async def fake_retrieve(instance_url, token, api_name, api_version="62.0"):
        return xml

    async def fake_list(instance_url, token, api_version="62.0"):
        from flowtool.sfdc import FlowSummary

        return flows if flows is not None else [
            FlowSummary("Existing_Flow", "Existing Flow", True, None, "2026-01-01")
        ]

    monkeypatch.setattr(server, "retrieve_flow", fake_retrieve)
    monkeypatch.setattr(server, "list_flows", fake_list)


class TestBrowseTheOrg:
    def test_lists_flows(self, client, monkeypatch):
        stub_org(monkeypatch)
        data = client.get("/api/flows").json()
        assert data["flows"][0]["api_name"] == "Existing_Flow"
        assert data["flows"][0]["active"] is True

    def test_imports_a_flow_as_a_diagram(self, client, scripted, monkeypatch):
        scripted(VALID)
        stub_org(monkeypatch)
        data = client.post("/api/import", json={"api_name": "Existing_Flow"}).json()

        assert data["imported"] is True
        assert data["api_name"] == "Existing_Flow"
        assert "Mark_Hot" in data["mermaid"]
        assert data["approved"] is False, "an imported flow still needs approving"

    def test_import_does_not_call_the_model(self, client, scripted, monkeypatch):
        provider = scripted()  # no payloads queued: any call would IndexError
        stub_org(monkeypatch)
        client.post("/api/import", json={"api_name": "Existing_Flow"})
        assert provider.calls == [], "importing should be pure parsing"

    def test_an_imported_active_flow_stays_active(self, client, scripted, monkeypatch):
        scripted(VALID)
        stub_org(monkeypatch)
        data = client.post("/api/import", json={"api_name": "Existing_Flow"}).json()
        # Opening a live flow to read it must not quietly propose deactivating it.
        assert data["status"] == "Active"

    def test_a_flow_we_cannot_model_is_refused_not_approximated(
        self, client, scripted, monkeypatch
    ):
        scripted(VALID)
        stub_org(monkeypatch, xml=SCREEN_XML)
        response = client.post("/api/import", json={"api_name": "Existing_Flow"})
        assert response.status_code == 422
        assert "screen elements" in response.json()["detail"]

    def test_an_imported_flow_can_be_refined(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        stub_org(monkeypatch)
        session_id = client.post(
            "/api/import", json={"api_name": "Existing_Flow"}
        ).json()["session_id"]

        refined = client.post(
            "/api/refine",
            json={"session_id": session_id, "instruction": "also set the description"},
        ).json()
        assert refined["version"] == 2

        # The model saw the existing flow before the instruction, so it edits
        # rather than designing a replacement.
        conversation = provider.calls[0]
        assert "Existing_Flow" in conversation[0].content
        assert "Keep everything about it the same" in conversation[0].content
        assert "Mark_Hot" in conversation[1].content, "the IR must precede the instruction"
        assert conversation[-1].content == "also set the description"


class TestExplain:
    def test_returns_prose_about_the_flow(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        provider.text = "It marks accounts hot."
        monkeypatch.setattr(
            type(provider), "complete_text",
            lambda self, system, messages: "It marks accounts hot.", raising=False,
        )
        session_id = design(client)["session_id"]
        data = client.post("/api/explain", json={"session_id": session_id}).json()
        assert data["explanation"] == "It marks accounts hot."

    def test_the_model_is_given_the_ir_not_the_xml(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        seen = {}

        def fake_text(self, system, messages):
            seen["content"] = messages[-1].content
            return "ok"

        monkeypatch.setattr(type(provider), "complete_text", fake_text, raising=False)
        session_id = design(client)["session_id"]
        client.post("/api/explain", json={"session_id": session_id})
        assert '"api_name"' in seen["content"], "expected the IR"
        assert "<Flow" not in seen["content"], "the XML is bigger and adds nothing"


class TestDeploy:
    def test_approved_and_confirmed_deploys_for_real(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})
        client.post("/api/validate", json={"session_id": session_id})

        result = client.post(
            "/api/deploy", json={"session_id": session_id, "confirm": True}
        ).json()
        assert result["success"] is True
        assert calls == [True, False], "expected checkOnly then a real deploy"
