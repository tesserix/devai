"""Contract tests for the GitHub transport + credential adapter.

Proves the two things the adapter promises: PAT and GitHub App are
interchangeable (same surface), and REST + GraphQL endpoints are derived
correctly for github.com and Enterprise.
"""

from __future__ import annotations

import asyncio

import pytest

from devai.scm.github_transport import (
    GitHubAppCredential,
    GitHubTransport,
    StaticTokenCredential,
    graphql_url_for,
)

# --------------------------------------------------------------------- #
# Endpoint derivation
# --------------------------------------------------------------------- #


def test_graphql_url_for_github_com() -> None:
    assert graphql_url_for("https://api.github.com") == "https://api.github.com/graphql"
    assert graphql_url_for("https://api.github.com/") == "https://api.github.com/graphql"


def test_graphql_url_for_enterprise() -> None:
    assert graphql_url_for("https://ghe.acme.com/api/v3") == "https://ghe.acme.com/api/graphql"


# --------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_static_token_returns_verbatim() -> None:
    cred = StaticTokenCredential("ghp_abc")
    assert await cred.token() == "ghp_abc"
    assert await cred.token() == "ghp_abc"  # stable across calls


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHTTP:
    """Records POSTs and returns a canned installation-token response."""

    def __init__(self, token: str = "ghs_inst", expires_at: str | None = None, delay: float = 0.0) -> None:
        self.posts = 0
        self._token = token
        self._expires_at = expires_at
        self._delay = delay

    async def post(self, path: str, **kwargs):  # noqa: ANN003
        self.posts += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        payload = {"token": self._token}
        if self._expires_at:
            payload["expires_at"] = self._expires_at
        return _FakeResp(payload)


def _app_cred(http: _FakeHTTP) -> GitHubAppCredential:
    cred = GitHubAppCredential(123, "unused-key", 456, http)  # type: ignore[arg-type]
    cred._jwt = lambda: "fake.jwt.token"  # type: ignore[method-assign] — skip RSA signing
    return cred


@pytest.mark.asyncio
async def test_app_credential_mints_then_caches() -> None:
    http = _FakeHTTP(expires_at="2999-01-01T00:00:00Z")  # far future → cache holds
    cred = _app_cred(http)
    t1 = await cred.token()
    t2 = await cred.token()
    assert t1 == t2 == "ghs_inst"
    assert http.posts == 1  # second call hit the cache


@pytest.mark.asyncio
async def test_app_credential_refreshes_when_near_expiry() -> None:
    http = _FakeHTTP(expires_at="2000-01-01T00:00:00Z")  # already expired → always refresh
    cred = _app_cred(http)
    await cred.token()
    await cred.token()
    assert http.posts == 2


@pytest.mark.asyncio
async def test_app_credential_single_flight_on_stampede() -> None:
    http = _FakeHTTP(expires_at="2999-01-01T00:00:00Z", delay=0.02)
    cred = _app_cred(http)
    tokens = await asyncio.gather(*(cred.token() for _ in range(10)))
    assert all(t == "ghs_inst" for t in tokens)
    assert http.posts == 1  # 10 concurrent callers, exactly one mint


# --------------------------------------------------------------------- #
# Transport wiring — PAT and App interchangeable
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transport_pat_auth_headers() -> None:
    t = GitHubTransport("https://api.github.com", token="ghp_pat")
    assert isinstance(t.credential, StaticTokenCredential)
    assert await t.auth_headers("token") == {"Authorization": "token ghp_pat"}
    assert await t.auth_headers("bearer") == {"Authorization": "bearer ghp_pat"}
    assert t.graphql_url == "https://api.github.com/graphql"
    await t.aclose()


@pytest.mark.asyncio
async def test_transport_app_selected_when_flagged() -> None:
    t = GitHubTransport(
        "https://api.github.com",
        use_github_app=True,
        app_id=1,
        private_key="k",
        installation_id=2,
    )
    assert isinstance(t.credential, GitHubAppCredential)
    await t.aclose()


@pytest.mark.asyncio
async def test_transport_empty_token_emits_no_auth_header() -> None:
    t = GitHubTransport("https://api.github.com", token="")
    assert await t.auth_headers("token") == {}
    await t.aclose()
