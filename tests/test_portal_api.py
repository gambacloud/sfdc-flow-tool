"""The /api/portal routes: validation, confirmation gates, and the job flow."""

import time

import pytest
from fastapi.testclient import TestClient

import server
from flowtool import portal_api
from flowtool.portal_preflight import Check, PreflightReport
from flowtool.portal_site import SiteResult, Step

ORG = {"instance_url": "https://x.my.salesforce.com", "access_token": "tok"}


def report(production=False, blocked=False):
    checks = [
        Check("org", "Target org", "warning", "fail" if production else "pass", "an org"),
        Check("licenses", "User licenses", "blocking", "fail" if blocked else "pass", "5 free"),
    ]
    return PreflightReport(checks)


@pytest.fixture
def client(monkeypatch):
    async def fake_preflight(org, spec):
        return fake_preflight.report

    fake_preflight.report = report()
    seen = {}

    async def fake_build(org, meta, spec, apply=False, progress=None):
        seen["spec"] = spec
        seen["apply"] = apply
        result = SiteResult(preflight=fake_preflight.report)
        if not apply:
            result.steps = [Step("Create site shell", "planned", "x")]
            return result
        for name in ("Create site shell", "Publish"):
            step = Step(name, "done", "")
            result.steps.append(step)
            if progress:
                progress(step)
        result.site_url = "https://x.my.site.com/hub"
        result.register_url = "https://x.my.site.com/hub/SelfRegister"
        return result

    class FakeMeta:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(portal_api, "run_preflight", fake_preflight)
    monkeypatch.setattr(portal_api, "build_site", fake_build)
    monkeypatch.setattr(portal_api, "MetadataClient", FakeMeta)
    c = TestClient(server.app)
    c.fake_preflight = fake_preflight
    c.seen = seen
    return c


def body(**kw):
    return {"site_name": "Support Hub", **ORG, **kw}


def test_preflight_returns_the_checklist(client):
    data = client.post("/api/portal/preflight", json=body()).json()
    assert data["can_proceed"] is True
    assert [c["id"] for c in data["checks"]] == ["org", "licenses"]


def test_plan_lists_the_steps_without_writing(client):
    data = client.post("/api/portal/plan", json=body()).json()
    assert data["steps"][0]["status"] == "planned"
    assert client.seen["apply"] is False


@pytest.mark.parametrize("name", ["", "  ", "x" * 80, "bad/name", "semi;colon", "<script>"])
def test_site_name_is_validated(client, name):
    assert client.post("/api/portal/preflight", json=body(site_name=name)).status_code == 422


def test_other_fields_are_validated(client):
    assert client.post("/api/portal/preflight", json=body(url_prefix="Bad Prefix!")).status_code == 422
    assert client.post("/api/portal/preflight", json=body(admin_username="not-an-email")).status_code == 422
    assert client.post("/api/portal/preflight", json=body(api_version="abc")).status_code == 422
    assert client.post("/api/portal/preflight", json=body(license="free_for_all")).status_code == 422


def test_apply_needs_confirmation(client):
    r = client.post("/api/portal/apply", json=body())
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    assert "apply" not in client.seen


def test_apply_on_a_production_org_needs_its_own_confirmation(client):
    client.fake_preflight.report = report(production=True)
    r = client.post("/api/portal/apply", json=body(confirm=True))
    assert r.status_code == 403 and "production" in r.json()["detail"]
    ok = client.post("/api/portal/apply", json=body(confirm=True, confirm_production=True))
    assert ok.status_code == 200 and "job_id" in ok.json()


def wait_for(client, job_id):
    for _ in range(100):
        data = client.get("/api/portal/status", params={"job_id": job_id}).json()
        if data["done"]:
            return data
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_apply_runs_as_a_job_and_reports_steps_and_urls(client):
    job = client.post("/api/portal/apply", json=body(confirm=True)).json()["job_id"]
    data = wait_for(client, job)
    assert data["ok"] is True
    assert [s["name"] for s in data["steps"]] == ["Create site shell", "Publish"]
    assert data["register_url"].endswith("/SelfRegister")
    assert client.seen["apply"] is True


def test_a_finished_job_is_forgotten(client):
    job = client.post("/api/portal/apply", json=body(confirm=True)).json()["job_id"]
    wait_for(client, job)
    assert client.get("/api/portal/status", params={"job_id": job}).status_code == 404


def test_unknown_job_is_a_404(client):
    assert client.get("/api/portal/status", params={"job_id": "nope"}).status_code == 404


def test_a_crashing_build_is_reported_not_raised(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("the org went away")

    monkeypatch.setattr(portal_api, "build_site", boom)
    job = client.post("/api/portal/apply", json=body(confirm=True)).json()["job_id"]
    data = wait_for(client, job)
    assert data["ok"] is False and "the org went away" in data["error"]


def test_the_request_becomes_the_right_spec(client):
    client.post("/api/portal/plan", json=body(
        headline="Welcome", intro="Hello there", chat=True, reuse=True, license="customer_community_plus",
        url_prefix="Hub", admin_username="a@b.co",
    ))
    spec = client.seen["spec"]
    assert spec.site_name == "Support Hub" and spec.prefix() == "hub"
    assert spec.license == "customer_community_plus" and spec.allow_existing_site and spec.want_miaw
    assert spec.content.headline == "Welcome" and spec.content.intro == "Hello there"
    assert spec.messaging.channel == "Support_Hub_Chat" and spec.messaging.queue == "Support_Hub_Support"


def test_no_headline_means_no_pages_and_no_chat_means_no_messaging(client):
    client.post("/api/portal/plan", json=body())
    spec = client.seen["spec"]
    assert spec.content is None and spec.messaging is None and not spec.want_miaw
