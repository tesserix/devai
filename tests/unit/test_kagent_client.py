"""Unit tests for the kagent A2A dispatch client (workstream G3)."""

from __future__ import annotations

import sys
import types

import pytest

from devai.agentic.kagent_client import KagentClient, KagentDispatchTarget, KagentError, create_kagent_client


def test_a2a_url_and_request_shape():
    c = KagentClient("http://kagent:8083/", namespace="kagent-system")
    assert c.a2a_url("reviewer") == "http://kagent:8083/api/a2a/kagent-system/reviewer"
    assert c.a2a_url("reviewer", namespace="other") == "http://kagent:8083/api/a2a/other/reviewer"

    req = KagentClient._build_request("do the thing", "m1", "r1")
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "message/send"
    assert req["id"] == "r1"
    msg = req["params"]["message"]
    assert msg["role"] == "user"
    assert msg["parts"] == [{"kind": "text", "text": "do the thing"}]
    assert msg["messageId"] == "m1"


def test_a2a_url_targets_substrate_sandbox_agent():
    c = KagentClient("http://kagent:8083/", namespace="kagent-system")

    assert (
        c.a2a_url("reviewer", target=KagentDispatchTarget.SANDBOX_AGENT)
        == "http://kagent:8083/api/a2a-sandboxes/kagent-system/reviewer"
    )


def test_create_kagent_client_disabled_when_no_url():
    class S:
        kagent_url = ""

    assert create_kagent_client(S()) is None

    class S2:
        kagent_url = "http://kagent:8083"
        kagent_default_namespace = "kagent-system"

    assert isinstance(create_kagent_client(S2()), KagentClient)


# ── dispatch (mocked httpx) ───────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeAsyncClient:
    """Records the last POST and returns a programmed response."""

    last_call = {}

    def __init__(self, timeout=None):
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.last_call = {"url": url, "json": json, "headers": headers}
        return _FakeAsyncClient._response


def _install_fake_httpx(monkeypatch, response):
    _FakeAsyncClient._response = response
    fake = types.ModuleType("httpx")
    fake.AsyncClient = _FakeAsyncClient
    monkeypatch.setitem(sys.modules, "httpx", fake)


@pytest.mark.asyncio
async def test_dispatch_success(monkeypatch):
    _install_fake_httpx(monkeypatch, _FakeResponse(200, {"jsonrpc": "2.0", "result": {"status": "ok"}}))
    c = KagentClient("http://kagent:8083", namespace="kagent-system")
    result = await c.dispatch("reviewer", "review PR #5", triggered_by="alice@corp.com", trace_id="t-123")

    assert result == {"status": "ok"}
    call = _FakeAsyncClient.last_call
    assert call["url"] == "http://kagent:8083/api/a2a/kagent-system/reviewer"
    assert call["json"]["params"]["message"]["parts"][0]["text"] == "review PR #5"
    assert call["headers"]["X-Forwarded-User"] == "alice@corp.com"
    assert call["headers"]["X-Trace-Id"] == "t-123"


@pytest.mark.asyncio
async def test_dispatch_forwards_api_key_as_bearer(monkeypatch):
    _install_fake_httpx(monkeypatch, _FakeResponse(200, {"jsonrpc": "2.0", "result": {}}))
    c = KagentClient("http://kagent:8083", namespace="kagent-system")
    await c.dispatch("reviewer", "x", api_key="sk-ant-user")
    assert _FakeAsyncClient.last_call["headers"]["Authorization"] == "Bearer sk-ant-user"


@pytest.mark.asyncio
async def test_dispatch_no_api_key_sends_no_authorization(monkeypatch):
    _install_fake_httpx(monkeypatch, _FakeResponse(200, {"jsonrpc": "2.0", "result": {}}))
    c = KagentClient("http://kagent:8083")
    await c.dispatch("reviewer", "x")
    assert "Authorization" not in _FakeAsyncClient.last_call["headers"]


@pytest.mark.asyncio
async def test_dispatch_http_error(monkeypatch):
    _install_fake_httpx(monkeypatch, _FakeResponse(500, text="boom"))
    c = KagentClient("http://kagent:8083")
    with pytest.raises(KagentError):
        await c.dispatch("reviewer", "x")


@pytest.mark.asyncio
async def test_dispatch_jsonrpc_error(monkeypatch):
    _install_fake_httpx(
        monkeypatch, _FakeResponse(200, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "nope"}})
    )
    c = KagentClient("http://kagent:8083")
    with pytest.raises(KagentError):
        await c.dispatch("reviewer", "x")
