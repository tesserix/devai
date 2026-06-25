"""MCP OAuth client — PKCE, discovery, DCR, code exchange + refresh, token mint."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from devai.settings import mcp_oauth
from devai.settings.mcp_oauth import (
    OAuthEndpoints,
    TokenSet,
    authorize_url,
    discover_endpoints,
    exchange_code,
    pkce_pair,
    refresh_access,
)


def test_pkce_pair_is_valid_s256():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_has_pkce_and_resource():
    ep = OAuthEndpoints(
        authorization_endpoint="https://as.example.com/authorize", token_endpoint="https://as.example.com/token"
    )
    url = authorize_url(
        ep,
        client_id="cid",
        redirect_uri="https://devai/cb",
        state="st",
        code_challenge="ch",
        scopes=["a", "b"],
        resource="https://mcp.example.com/",
    )
    assert url.startswith("https://as.example.com/authorize?")
    assert "code_challenge=ch" in url and "code_challenge_method=S256" in url
    assert "resource=" in url and "scope=a+b" in url


def test_tokenset_expiry():
    ts = TokenSet.from_response({"access_token": "a", "expires_in": 3600, "refresh_token": "r"}, now=1000.0)
    assert ts.expired(now=1000.0) is False
    assert ts.expired(now=1000.0 + 3600) is True  # past expiry (minus the 60s skew)
    assert TokenSet(access_token="").expired(now=0) is True


class _Resp:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, routes: dict[str, Any], sink: list[tuple[str, str, Any]]):
        self._routes = routes
        self._sink = sink

    async def get(self, url: str, **kw):
        self._sink.append(("GET", url, None))
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404, {})

    async def post(self, url: str, **kw):
        self._sink.append(("POST", url, kw.get("data") or kw.get("json")))
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404, {})


async def test_discovery_protected_resource_then_as_metadata():
    sink: list = []
    routes = {
        "/.well-known/oauth-protected-resource": _Resp(200, {"authorization_servers": ["https://as.example.com"]}),
        "/.well-known/oauth-authorization-server": _Resp(
            200,
            {
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register",
            },
        ),
    }
    ep = await discover_endpoints(_Client(routes, sink), "https://mcp.example.com/mcp")
    assert ep is not None
    assert ep.token_endpoint == "https://as.example.com/token"
    assert ep.registration_endpoint == "https://as.example.com/register"


async def test_discovery_returns_none_when_no_metadata():
    ep = await discover_endpoints(_Client({}, []), "https://mcp.example.com/mcp")
    assert ep is None


async def test_exchange_and_refresh():
    sink: list = []
    routes = {"/token": _Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})}
    client = _Client(routes, sink)
    ts = await exchange_code(
        client,
        "https://as/token",
        code="c",
        redirect_uri="https://devai/cb",
        client_id="cid",
        code_verifier="v",
        now=0.0,
    )
    assert ts.access_token == "AT" and ts.refresh_token == "RT"
    # the exchange posted the PKCE verifier + code
    posted = sink[-1][2]
    assert posted["grant_type"] == "authorization_code" and posted["code_verifier"] == "v"

    routes["/token"] = _Resp(200, {"access_token": "AT2", "expires_in": 3600})  # no rotation
    ts2 = await refresh_access(client, "https://as/token", refresh_token="RT", client_id="cid", now=0.0)
    assert ts2.access_token == "AT2"
    assert ts2.refresh_token == "RT"  # kept the old refresh token when not rotated


async def test_access_token_cache_and_refresh(monkeypatch):
    calls = {"n": 0}

    async def fake_refresh(client, token_endpoint, *, refresh_token, client_id, client_secret="", now):
        calls["n"] += 1
        return TokenSet(access_token=f"AT{calls['n']}", refresh_token=refresh_token, expires_at=now + 3600)

    # Patch httpx so access_token's AsyncClient context works, and the refresh.
    import httpx

    class _NoopClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _NoopClient())
    monkeypatch.setattr(mcp_oauth, "refresh_access", fake_refresh)
    mcp_oauth._TOKEN_CACHE.clear()

    t1 = await mcp_oauth.access_token(refresh_token="R", token_endpoint="https://as/token", client_id="c", now=1000.0)
    t2 = await mcp_oauth.access_token(refresh_token="R", token_endpoint="https://as/token", client_id="c", now=1000.0)
    assert t1 == "AT1" and t2 == "AT1"  # cached, one refresh
    assert calls["n"] == 1
    # past expiry → refreshes again
    t3 = await mcp_oauth.access_token(
        refresh_token="R", token_endpoint="https://as/token", client_id="c", now=1000.0 + 4000
    )
    assert t3 == "AT2" and calls["n"] == 2


async def test_access_token_missing_inputs_returns_empty():
    assert await mcp_oauth.access_token(refresh_token="", token_endpoint="t", client_id="c", now=0.0) == ""
