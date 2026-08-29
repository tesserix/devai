from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.openpanel import fetch_overview


def _config(**kw):
    base = {
        "openpanel_api_url": "https://analytics.example.com/api",
        "openpanel_client_id": "cid",
        "openpanel_client_secret": "secret",
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_disabled_without_api_url():
    out = await fetch_overview(_config(openpanel_api_url=""), 30)
    assert out == {"enabled": False, "reason": "not configured"}


@pytest.mark.asyncio
async def test_disabled_without_client_id():
    out = await fetch_overview(_config(openpanel_client_id=""), 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_disabled_without_secret():
    out = await fetch_overview(_config(openpanel_client_secret=""), 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_disabled_with_no_config_object():
    out = await fetch_overview(None, 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_returns_payload_when_configured(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"visitors": 42, "sessions": 60}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await fetch_overview(_config(), 7)
    assert out["enabled"] is True
    assert out["visitors"] == 42
    assert captured["params"]["days"] == 7
    # The secret authenticates server-side and must never reach the browser.
    assert captured["headers"]["openpanel-client-secret"] == "secret"


@pytest.mark.asyncio
async def test_upstream_failure_degrades_to_disabled(monkeypatch):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_kw):
            raise RuntimeError("unreachable")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await fetch_overview(_config(), 30)
    assert out["enabled"] is False
    assert "unavailable" in out["reason"]
