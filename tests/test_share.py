"""
"Share this" - snapshot a Session/PlanSession, hand back a link+password,
resume from it as a brand-new session. Redis itself is faked in-process
(no real Upstash call) so this stays as fast and deterministic as the rest
of the suite; flowtool/share.py's own hashing/rate-limit logic runs for real.
"""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool import share
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_llm import ScriptedProvider, TypedScriptedProvider, VALID as VALID_FLOW
from tests.test_server_plan import BUNDLE_PLAN, make_plan, execute
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT


@pytest.fixture
def fake_redis(monkeypatch):
    """An in-memory stand-in for Upstash - same three primitives share.py
    calls, none of the HTTP plumbing."""
    store: dict = {}

    async def fake_set(key, value, ex):
        store[key] = value

    async def fake_get(key):
        return store.get(key)

    async def fake_incr(key, ex):
        store[key] = str(int(store.get(key, "0")) + 1)
        return int(store[key])

    monkeypatch.setattr(share, "_redis_set", fake_set)
    monkeypatch.setattr(share, "_redis_get", fake_get)
    monkeypatch.setattr(share, "_redis_incr_with_expiry", fake_incr)
    monkeypatch.setattr(share, "configured", lambda: True)
    return store


@pytest.fixture
def client(monkeypatch):
    server.SESSIONS.clear()
    server.PLAN_SESSIONS.clear()
    monkeypatch.setattr(
        server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok")
    )
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def scripted(monkeypatch):
    def install(*payloads):
        provider = ScriptedProvider(*payloads)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


@pytest.fixture
def typed_scripted(monkeypatch):
    def install(**payloads_by_type):
        provider = TypedScriptedProvider(**payloads_by_type)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


def poll(client, url, **params):
    for _ in range(50):
        response = client.get(url, params=params)
        if response.status_code != 200 or response.json().get("done"):
            return response
    raise AssertionError(f"{url} never completed")


def design(client, **body):
    started = client.post("/api/design/start", json={"request": "build it", **body})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    return poll(client, "/api/design/status", job_id=job_id).json()


class TestShareNotConfigured:
    def test_config_says_disabled_without_upstash_vars(self, client, monkeypatch):
        monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
        monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
        assert client.get("/api/config").json()["share_enabled"] is False

    def test_share_start_fails_cleanly(self, client, scripted, monkeypatch):
        monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
        monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
        scripted(VALID_FLOW)
        data = design(client)
        resp = client.post(
            "/api/share/start", json={"kind": "session", "session_id": data["session_id"]}
        )
        assert resp.status_code == 400
        assert "not configured" in resp.text.lower() or "isn't configured" in resp.text.lower()


class TestShareSession:
    def test_round_trip_resumes_ready_to_approve(self, client, scripted, fake_redis):
        scripted(VALID_FLOW)
        original = design(client)
        assert original["approved"] is False

        started = client.post(
            "/api/share/start", json={"kind": "session", "session_id": original["session_id"]}
        )
        assert started.status_code == 200, started.text
        token, password = started.json()["token"], started.json()["password"]

        unlocked = client.post(f"/api/share/{token}/unlock", json={"password": password})
        assert unlocked.status_code == 200, unlocked.text
        data = unlocked.json()
        assert data["share_kind"] == "session"
        assert data["approved"] is False
        assert data["api_name"] == original["api_name"]
        # A fresh, independent session id - not the same one being reused.
        assert data["session_id"] != original["session_id"]
        assert data["session_id"] in server.SESSIONS

    def test_wrong_password_is_rejected_and_rate_limited(self, client, scripted, fake_redis):
        scripted(VALID_FLOW)
        original = design(client)
        started = client.post(
            "/api/share/start", json={"kind": "session", "session_id": original["session_id"]}
        )
        token = started.json()["token"]

        for _ in range(5):
            resp = client.post(f"/api/share/{token}/unlock", json={"password": "000000"})
            assert resp.status_code == 400

        # Locked out now, even with the right password.
        real_password = started.json()["password"]
        locked = client.post(f"/api/share/{token}/unlock", json={"password": real_password})
        assert locked.status_code == 400
        assert "too many" in locked.text.lower()

    def test_unknown_token_is_rejected(self, client, fake_redis):
        resp = client.post("/api/share/does-not-exist/unlock", json={"password": "123456"})
        assert resp.status_code == 400

    def test_a_resumed_session_can_be_approved_and_refined(self, client, scripted, fake_redis):
        scripted(VALID_FLOW)
        original = design(client)
        started = client.post(
            "/api/share/start", json={"kind": "session", "session_id": original["session_id"]}
        )
        token, password = started.json()["token"], started.json()["password"]
        data = client.post(f"/api/share/{token}/unlock", json={"password": password}).json()
        session_id = data["session_id"]

        approved = client.post(
            "/api/approve", json={"session_id": session_id, "version": data["version"]}
        )
        assert approved.status_code == 200
        assert approved.json()["approved"] is True


class TestSharePlan:
    def test_round_trip_resumes_the_whole_bundle(self, client, typed_scripted, fake_redis):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client)
        original = execute(client, plan["plan_id"])
        assert original["approved"] is False
        assert len(original["steps"]) == 3

        started = client.post(
            "/api/share/start", json={"kind": "plan", "session_id": original["session_id"]}
        )
        assert started.status_code == 200, started.text
        token, password = started.json()["token"], started.json()["password"]

        unlocked = client.post(f"/api/share/{token}/unlock", json={"password": password})
        assert unlocked.status_code == 200, unlocked.text
        data = unlocked.json()
        assert data["share_kind"] == "plan"
        assert data["approved"] is False
        assert data["session_id"] != original["session_id"]
        assert data["session_id"] in server.PLAN_SESSIONS

        by_name = {s["name"]: s for s in data["steps"]}
        assert by_name["Object"]["api_name"] == "Invoice__c"
        assert by_name["Field"]["object_api_name"] == "Invoice__c"
        assert "class" in by_name["Apex"]["body"]
