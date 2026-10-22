"""Privacy page and the rate limit on LLM-spending endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from flowtool import ratelimit

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client(monkeypatch):
    for var in (ratelimit.MIN_ENV, ratelimit.DAY_ENV, server.hosted.HOSTED_ENV):
        monkeypatch.delenv(var, raising=False)
    ratelimit.reset()
    with TestClient(server.app) as c:
        yield c
    ratelimit.reset()


def test_privacy_page_served_as_standalone_html(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1>Privacy Policy: SFDC Build Tool (Chrome extension)</h1>" in r.text
    assert "<script" not in r.text and "src=" not in r.text


def test_packaged_policy_matches_docs_copy():
    assert (ROOT / "docs/store/PRIVACY_POLICY.md").read_bytes() == (
        ROOT / "flowtool/legal/PRIVACY_POLICY.md"
    ).read_bytes()
    assert "legal/*.md" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_limit_applies_per_client_and_returns_message(client, monkeypatch):
    monkeypatch.setenv(ratelimit.MIN_ENV, "2")
    headers = {"X-Client-Id": "a", "X-Forwarded-For": "1.1.1.1"}
    codes = [client.post("/api/design/start", json={}, headers=headers).status_code for _ in range(3)]
    assert 429 not in codes[:2] and codes[2] == 429
    r = client.post("/api/design/start", json={}, headers=headers)
    assert "per minute" in r.json()["detail"] and r.headers["retry-after"]
    other = {"X-Client-Id": "b", "X-Forwarded-For": "2.2.2.2"}
    assert client.post("/api/design/start", json={}, headers=other).status_code != 429


def test_daily_limit_and_disable(client, monkeypatch):
    monkeypatch.setenv(ratelimit.MIN_ENV, "0")
    monkeypatch.setenv(ratelimit.DAY_ENV, "1")
    h = {"X-Client-Id": "a"}
    assert client.post("/api/refine/start", json={}, headers=h).status_code != 429
    r = client.post("/api/refine/start", json={}, headers=h)
    assert r.status_code == 429 and "Daily" in r.json()["detail"]
    monkeypatch.setenv(ratelimit.DAY_ENV, "0")
    assert client.post("/api/refine/start", json={}, headers=h).status_code != 429


def test_unlimited_paths_and_methods_untouched(client, monkeypatch):
    monkeypatch.setenv(ratelimit.MIN_ENV, "1")
    for _ in range(5):
        assert client.get("/api/design/status", params={"job_id": "x"}).status_code != 429
        assert client.get("/privacy").status_code == 200


def test_ip_bucket_stops_rotating_client_ids(client, monkeypatch):
    monkeypatch.setenv(ratelimit.MIN_ENV, "1")
    codes = [
        client.post("/api/design/start", json={}, headers={"X-Client-Id": str(i), "X-Forwarded-For": "9.9.9.9"}).status_code
        for i in range(ratelimit.IP_FACTOR + 1)
    ]
    assert codes[-1] == 429 and 429 not in codes[:-1]


def test_client_ip_uses_last_forwarded_hop():
    class R:
        headers = {"x-forwarded-for": "6.6.6.6, 7.7.7.7"}
        client = None

    assert ratelimit.client_ip(R()) == "7.7.7.7"


EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def test_extension_origin_must_send_own_key(client, monkeypatch):
    monkeypatch.setenv(server.hosted.CORS_ENV, EXT)
    h = {"Origin": EXT, "X-Client-Id": "a"}
    for body in ({}, {"api_key": ""}, {"api_key": "  "}):
        r = client.post("/api/design/start", json=body, headers=h)
        assert r.status_code == 400
        assert r.json()["detail"] == ratelimit.KEY_MESSAGE
    assert client.post("/api/design/start", json={"api_key": "k"}, headers=h).status_code != 400


def test_web_ui_and_other_origins_keep_server_key_fallback(client, monkeypatch):
    monkeypatch.setenv(server.hosted.CORS_ENV, EXT)
    for h in ({}, {"Origin": "https://evil.example"}):
        assert client.post("/api/design/start", json={}, headers=h).status_code != 400 or \
            client.post("/api/design/start", json={}, headers=h).json()["detail"] != ratelimit.KEY_MESSAGE


def test_key_required_only_on_llm_endpoints(client, monkeypatch):
    monkeypatch.setenv(server.hosted.CORS_ENV, EXT)
    r = client.get("/api/design/status", params={"job_id": "x"}, headers={"Origin": EXT})
    assert r.status_code != 400 or r.json()["detail"] != ratelimit.KEY_MESSAGE
