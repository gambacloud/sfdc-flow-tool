"""
The HTTP surface, with the approval gate tested server-side.

A gate that lives only in the browser is not a gate: anyone can call the
endpoint directly. These assert the server refuses on its own.
"""

import pytest
from fastapi.testclient import TestClient

import server
from flowforge.llm import FlowGenerator
from flowforge.sfdc import ComponentProblem, DeployResult
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
