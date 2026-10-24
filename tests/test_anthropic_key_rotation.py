"""A server-held Anthropic key the API rejects falls through to the next ANTHROPIC_API_KEY<n>;
a key the person typed never does."""

import anthropic
import httpx
import pytest

from flowtool import llm
from flowtool.llm import AnthropicProvider, LLMError


def auth_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=request), body=None)


class FakeClient:
    calls = []

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.messages = self
        self.models = self

    def create(self, **kwargs):
        FakeClient.calls.append(self.api_key)
        if self.api_key.startswith("bad"):
            raise auth_error()
        return {"ok": self.api_key}

    def list(self, limit=100):
        FakeClient.calls.append(self.api_key)
        if self.api_key.startswith("bad"):
            raise auth_error()
        return []


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeClient.calls = []
    llm._REJECTED_ANTHROPIC_KEYS.clear()
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY2", "ANTHROPIC_API_KEY3"):
        monkeypatch.delenv(name, raising=False)
    yield
    llm._REJECTED_ANTHROPIC_KEYS.clear()


def provider(**kw):
    return AnthropicProvider(model="claude-sonnet-5-5", **kw)


def test_numbered_keys_are_collected_in_order(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("ANTHROPIC_API_KEY2", "b")
    monkeypatch.setenv("ANTHROPIC_API_KEY3", "c")
    assert llm._anthropic_keys() == ["a", "b", "c"]


def test_a_rejected_first_key_falls_through_to_the_second(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY2", "good-2")
    assert provider()._create(messages=[]) == {"ok": "good-2"}
    assert FakeClient.calls == ["bad-1", "good-2"]


def test_a_rejected_key_is_not_tried_again_by_the_next_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY2", "good-2")
    provider()._create(messages=[])
    FakeClient.calls = []
    provider()._create(messages=[])
    assert FakeClient.calls == ["good-2"]


def test_when_every_key_is_rejected_the_error_is_the_usual_one(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY2", "bad-2")
    with pytest.raises(LLMError, match="rejected the API key"):
        provider()._create(messages=[])
    assert FakeClient.calls == ["bad-1", "bad-2"]


def test_a_key_the_person_typed_is_never_replaced_by_a_server_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "good-1")
    with pytest.raises(LLMError, match="rejected the API key"):
        provider(api_key="bad-mine")._create(messages=[])
    assert FakeClient.calls == ["bad-mine"]
    assert "bad-mine" not in llm._REJECTED_ANTHROPIC_KEYS


def test_listing_models_also_skips_a_rejected_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY2", "good-2")
    assert provider().list_models() == []
    assert FakeClient.calls == ["bad-1", "good-2"]
