"""Scratch org creation through the sf CLI. The CLI is faked; no org or network is used."""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool import orgs
from flowtool.orgs import SfCliError

LISTING = {
    "devHubs": [{"alias": "hub", "username": "hub@example.com"}],
    "scratchOrgs": [{"alias": "mine", "username": "u@x.org", "expirationDate": "2026-12-01", "isExpired": False}],
}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake(args, timeout=120):
        seen.append(args)
        if args[:2] == ["org", "list"]:
            return LISTING
        if args[:3] == ["org", "create", "scratch"]:
            return {"username": "new@x.org", "instanceUrl": "https://new.my.salesforce.com", "expirationDate": "2026-11-09"}
        return {}

    monkeypatch.setattr(orgs, "_run_sf", fake)
    return seen


@pytest.fixture
def client(monkeypatch):
    for var in (server.hosted.HOSTED_ENV, server.hosted.CORS_ENV):
        monkeypatch.delenv(var, raising=False)
    return TestClient(server.app)


def test_create_passes_name_lifetime_edition_and_the_dev_hub(calls):
    out = orgs.create_scratch_org("feature-1", 3, "enterprise", "hub")
    args = next(a for a in calls if a[:3] == ["org", "create", "scratch"])
    assert args[args.index("--alias") + 1] == "feature-1"
    assert args[args.index("--duration-days") + 1] == "3"
    assert args[args.index("--edition") + 1] == "enterprise"
    assert args[args.index("--target-dev-hub") + 1] == "hub"
    assert out["alias"] == "feature-1" and out["expires"] == "2026-11-09"


def test_without_a_named_dev_hub_the_first_connected_one_is_used(calls):
    orgs.create_scratch_org("a1", 7, "developer", None)
    args = next(a for a in calls if a[:3] == ["org", "create", "scratch"])
    assert args[args.index("--target-dev-hub") + 1] == "hub"


@pytest.mark.parametrize(
    "alias,days,edition",
    [("-rf", 7, "developer"), ("has space", 7, "developer"), ("", 7, "developer"),
     ("ok", 0, "developer"), ("ok", 31, "developer"), ("ok", 7, "bogus")],
)
def test_bad_input_is_refused_before_the_cli_runs(calls, alias, days, edition):
    with pytest.raises(SfCliError):
        orgs.create_scratch_org(alias, days, edition, "hub")
    assert calls == []


def test_delete_only_touches_a_known_scratch_org(calls):
    with pytest.raises(SfCliError):
        orgs.delete_scratch_org("hub")
    assert not any(a[:2] == ["org", "delete"] for a in calls)
    orgs.delete_scratch_org("mine")
    assert any(a[:3] == ["org", "delete", "scratch"] for a in calls)


def test_endpoints_create_then_report_the_org(calls, client):
    start = client.post("/api/scratch/start", json={"alias": "e2e", "duration_days": 2})
    assert start.status_code == 200
    job = start.json()["job_id"]
    for _ in range(100):
        body = client.get("/api/scratch/status", params={"job_id": job}).json()
        if body["done"]:
            break
    assert body["org"]["alias"] == "e2e"
    assert server.SCRATCH_ALIASES == {}


def test_delete_endpoint_needs_confirmation(calls, client):
    assert client.post("/api/scratch/delete", json={"alias": "mine"}).status_code == 400
    assert client.post("/api/scratch/delete", json={"alias": "mine", "confirm": True}).status_code == 200


def test_hosted_mode_refuses_scratch_endpoints(calls, client, monkeypatch):
    monkeypatch.setenv(server.hosted.HOSTED_ENV, "1")
    assert client.get("/api/scratch/info").status_code == 403
    assert client.post("/api/scratch/start", json={"alias": "x"}).status_code == 403
