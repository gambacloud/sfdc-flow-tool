"""
The /api/plan/* HTTP surface - the multi-artifact path added alongside the
existing single-Flow /api/design/* one. Same approval-gate discipline as
test_server.py's TestApprovalGate: enforced server-side, not decoration.
"""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.sfdc import ComponentProblem, DeployResult
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_llm import ScriptedProvider, VALID as VALID_FLOW

ONE_STEP_PLAN = {
    "steps": [
        {"artifact_type": "flow", "name": "Won Deal Flow", "brief": "build it"},
    ]
}

BUNDLE_PLAN = {
    "steps": [
        {"artifact_type": "object", "name": "Object", "brief": "an Invoice object"},
        {"artifact_type": "field", "name": "Field", "brief": "an Amount field",
         "depends_on": ["Object"]},
        {"artifact_type": "apex", "name": "Apex", "brief": "a helper class"},
    ]
}


@pytest.fixture
def client(monkeypatch):
    server.PLAN_JOBS.clear()
    server.PLANS.clear()
    server.PLAN_EXECUTIONS.clear()
    server.PLAN_SESSIONS.clear()
    monkeypatch.setattr(
        server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok")
    )
    # Context-managed rather than a bare TestClient(): a plan can involve
    # several background create_task()+to_thread() calls in one test (plan,
    # execute, validate, deploy), and without an explicit portal lifetime
    # some of those raced with the next test's client under BaseHTTPMiddleware
    # (surfaced as anyio.EndOfStream / "No response returned" - a TestClient
    # lifecycle issue, not a bug in the endpoints themselves).
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def scripted(monkeypatch):
    def install(*payloads):
        provider = ScriptedProvider(*payloads)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


def poll(client, url, **params):
    for _ in range(50):
        response = client.get(url, params=params)
        if response.status_code != 200 or response.json().get("done"):
            return response
    raise AssertionError(f"{url} never completed")


def make_plan(client, *payloads, request="build it"):
    """Drive /api/plan/start + status, returning the plan_id and steps."""
    started = client.post("/api/plan/start", json={"request": request})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    response = poll(client, "/api/plan/status", job_id=job_id)
    assert response.status_code == 200, response.text
    return response.json()


def execute(client, plan_id):
    started = client.post("/api/plan/execute/start", json={"plan_id": plan_id})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    response = poll(client, "/api/plan/execute/status", job_id=job_id)
    assert response.status_code == 200, response.text
    return response.json()


def full_session(client, scripted, *payloads, request="build it"):
    """One-step convenience: plan -> execute, in one scripted provider."""
    scripted(*payloads)
    plan = make_plan(client, request=request)
    return execute(client, plan["plan_id"])


class TestPlanStart:
    def test_a_flow_only_request_is_a_one_step_plan(self, client, scripted):
        scripted(ONE_STEP_PLAN)
        plan = make_plan(client)
        assert len(plan["steps"]) == 1
        assert plan["steps"][0]["artifact_type"] == "flow"

    def test_empty_request_is_rejected(self, client, scripted):
        scripted(ONE_STEP_PLAN)
        assert client.post("/api/plan/start", json={"request": "  "}).status_code == 400

    def test_unknown_plan_id_is_rejected(self, client):
        assert client.post(
            "/api/plan/execute/start", json={"plan_id": "nope"}
        ).status_code == 404


class TestPlanExecute:
    def test_bundle_plan_runs_every_generator(self, client, scripted):
        scripted(BUNDLE_PLAN, VALID_OBJECT, VALID_FIELD, VALID_APEX)
        plan = make_plan(client)
        session = execute(client, plan["plan_id"])

        assert session["version"] == 1
        assert session["approved"] is False
        by_name = {s["name"]: s for s in session["steps"]}
        assert by_name["Object"]["artifact_type"] == "object"
        assert by_name["Object"]["api_name"] == "Invoice__c"
        assert by_name["Field"]["artifact_type"] == "field"
        assert by_name["Field"]["object_api_name"] == "Invoice__c"
        assert by_name["Apex"]["artifact_type"] == "apex"
        assert "class" in by_name["Apex"]["body"]

    def test_single_flow_step_carries_a_diagram(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        assert "flowchart TD" in session["steps"][0]["mermaid"]

    def test_a_plan_can_only_be_executed_once(self, client, scripted):
        scripted(ONE_STEP_PLAN, VALID_FLOW)
        plan = make_plan(client)
        execute(client, plan["plan_id"])
        again = client.post("/api/plan/execute/start", json={"plan_id": plan["plan_id"]})
        assert again.status_code == 404


class TestPlanApprovalGate:
    def test_validate_before_approval_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post("/api/plan/validate/start", json={"session_id": session["session_id"]})
        assert response.status_code == 403

    def test_deploy_before_approval_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post(
            "/api/plan/deploy/start",
            json={"session_id": session["session_id"], "confirm": True},
        )
        assert response.status_code == 403

    def test_approving_a_stale_version_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post(
            "/api/plan/approve",
            json={"session_id": session["session_id"], "version": session["version"] + 1},
        )
        assert response.status_code == 409

    def test_approve_then_validate_succeeds(self, client, scripted, monkeypatch):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        sid = session["session_id"]

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            assert "Flow" in types
            return DeployResult("1", "Succeeded", True)

        monkeypatch.setattr(server, "validate_bundle", fake_validate_bundle)

        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        started = client.post("/api/plan/validate/start", json={"session_id": sid})
        assert started.status_code == 200, started.text
        result = poll(client, "/api/plan/validate/status", session_id=sid)
        assert result.json()["success"] is True


class TestPlanDeploy:
    def test_deploy_needs_explicit_confirmation(self, client, scripted, monkeypatch):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        response = client.post("/api/plan/deploy/start", json={"session_id": sid})
        assert response.status_code == 400

    def test_confirmed_deploy_bundles_every_step(self, client, scripted, monkeypatch):
        scripted(BUNDLE_PLAN, VALID_OBJECT, VALID_FIELD, VALID_APEX)
        plan = make_plan(client)
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]

        seen_types = {}

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            seen_types.update(types)
            return DeployResult("1", "Succeeded", True)

        monkeypatch.setattr(server, "validate_bundle", fake_validate_bundle)

        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        started = client.post(
            "/api/plan/deploy/start", json={"session_id": sid, "confirm": True}
        )
        assert started.status_code == 200, started.text
        result = poll(client, "/api/plan/deploy/status", session_id=sid)
        assert result.json()["success"] is True
        assert set(seen_types) == {"CustomObject", "CustomField", "ApexClass"}


class TestPlanSessionView:
    def test_session_can_be_refetched(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        again = client.get(f"/api/plan/session/{session['session_id']}")
        assert again.status_code == 200
        assert again.json()["steps"][0]["artifact_type"] == "flow"

    def test_unknown_session_is_404(self, client):
        assert client.get("/api/plan/session/nope").status_code == 404


class TestPlanRepair:
    def test_repair_without_a_failure_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post("/api/plan/repair/start", json={"session_id": session["session_id"]})
        assert response.status_code == 400

    def test_repair_regenerates_only_the_step_a_failure_names(self, client, scripted, monkeypatch):
        # Queue the bundle plan, its three step generations, then the fixed
        # field payload the repair round should ask for.
        scripted(BUNDLE_PLAN, VALID_OBJECT, VALID_FIELD, VALID_APEX, VALID_FIELD)
        plan = make_plan(client)
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            return DeployResult(
                "1", "Failed", False,
                failures=[ComponentProblem(
                    "Amount__c", "Field does not exist: Amount__c on Invoice__c", "Error",
                )],
            )

        monkeypatch.setattr(server, "validate_bundle", fake_validate_bundle)
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        client.post("/api/plan/validate/start", json={"session_id": sid})
        validated = poll(client, "/api/plan/validate/status", session_id=sid)
        assert validated.json()["success"] is False

        started = client.post("/api/plan/repair/start", json={"session_id": sid})
        assert started.status_code == 200, started.text
        repaired = poll(client, "/api/plan/repair/status", session_id=sid)
        assert repaired.status_code == 200, repaired.text
        data = repaired.json()

        # A new version, not yet approved - the same "repair revokes
        # approval" discipline test_server.py pins for the single-Flow path.
        assert data["version"] == session["version"] + 1
        assert data["approved"] is False

        by_name = {s["name"]: s for s in data["steps"]}
        assert by_name["Object"]["repairs"] == 0, "untouched step should not be re-run"
        assert by_name["Apex"]["repairs"] == 0, "untouched step should not be re-run"
        # Only the Field generator's queued payload was consumed for the
        # repair - if Object or Apex had also been re-run, this would be off.

    def test_repairing_stale_failures_after_a_successful_validate_is_rejected(
        self, client, scripted, monkeypatch
    ):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        sid = session["session_id"]

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            return DeployResult("1", "Succeeded", True)

        monkeypatch.setattr(server, "validate_bundle", fake_validate_bundle)
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        client.post("/api/plan/validate/start", json={"session_id": sid})
        poll(client, "/api/plan/validate/status", session_id=sid)

        response = client.post("/api/plan/repair/start", json={"session_id": sid})
        assert response.status_code == 400


class TestPlanStepRevise:
    def test_empty_instruction_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post(
            "/api/plan/step/revise/start",
            json={"session_id": session["session_id"], "step_name": "Won Deal Flow",
                  "instruction": "  "},
        )
        assert response.status_code == 400

    def test_unknown_step_name_is_rejected(self, client, scripted):
        session = full_session(client, scripted, ONE_STEP_PLAN, VALID_FLOW)
        response = client.post(
            "/api/plan/step/revise/start",
            json={"session_id": session["session_id"], "step_name": "Ghost",
                  "instruction": "change it"},
        )
        assert response.status_code == 404

    def test_revise_regenerates_only_the_named_step(self, client, scripted):
        # Bundle plan + its three generations, then one more Field payload
        # for the revision itself.
        scripted(BUNDLE_PLAN, VALID_OBJECT, VALID_FIELD, VALID_APEX, VALID_FIELD)
        plan = make_plan(client)
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]

        started = client.post(
            "/api/plan/step/revise/start",
            json={"session_id": sid, "step_name": "Field",
                  "instruction": "make it a Currency field instead"},
        )
        assert started.status_code == 200, started.text
        revised = poll(client, "/api/plan/step/revise/status", session_id=sid)
        assert revised.status_code == 200, revised.text
        data = revised.json()

        assert data["version"] == session["version"] + 1
        assert data["approved"] is False  # revising revokes approval
        by_name = {s["name"]: s for s in data["steps"]}
        assert by_name["Object"]["repairs"] == 0, "untouched step should not be re-run"
        assert by_name["Apex"]["repairs"] == 0, "untouched step should not be re-run"

    def test_revise_can_run_before_any_validate(self, client, scripted):
        # No approve/validate at all - proves this is the proactive path,
        # not gated behind having already failed a deploy like repair is.
        scripted(ONE_STEP_PLAN, VALID_FLOW, VALID_FLOW)
        plan = make_plan(client)
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]

        started = client.post(
            "/api/plan/step/revise/start",
            json={"session_id": sid, "step_name": "Won Deal Flow",
                  "instruction": "also mark the account hot"},
        )
        assert started.status_code == 200, started.text
        revised = poll(client, "/api/plan/step/revise/status", session_id=sid)
        assert revised.status_code == 200, revised.text
