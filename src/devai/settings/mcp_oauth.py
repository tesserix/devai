"""MCP OAuth 2.1 client — connect hosted SaaS MCP servers per user.

Implements the MCP authorization flow (OAuth 2.1 + PKCE, RFC 8414 / RFC 9728
metadata discovery, RFC 7591 dynamic client registration) so a user can
one-click-connect GitHub, Jira/Atlassian, Notion, Linear, Slack, … from the
marketplace. The long-lived refresh token is stored in THAT user's GCP SM
scope; access tokens are minted on demand and refreshed when expired.

Everything here is pure-ish (httpx + hashlib + secrets) so it unit-tests with
a mocked HTTP client. The routes (settings/oauth_routes.py) drive it: /start
builds the consent URL, /callback exchanges the code and provisions the
connector; personal-leg federation resolves a fresh Bearer via access_token().

Two ways the OAuth endpoints are found:
  1. Discovery — fetch the server's protected-resource metadata, then the
     authorization-server metadata (the spec's happy path; DCR-capable).
  2. Catalog override — the catalog seed's ``connect.oauth`` block carries the
     endpoints/scopes/client for providers that don't support discovery/DCR.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

logger = logging.getLogger(__name__)


# ── PKCE ─────────────────────────────────────────────────────────────────────


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def random_state() -> str:
    return secrets.token_urlsafe(24)


# ── metadata ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class OAuthEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    scopes: list[str] = field(default_factory=list)


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


async def discover_endpoints(client: Any, server_url: str) -> OAuthEndpoints | None:
    """RFC 9728 → RFC 8414 discovery from the MCP server's origin.

    Best-effort: tries protected-resource metadata to find the auth server,
    then the auth-server metadata for the endpoints. Falls back to probing the
    server origin's well-knowns directly. Returns None when nothing resolves.
    """
    origin = _origin(server_url)
    as_url = origin
    try:
        r = await client.get(f"{origin}/.well-known/oauth-protected-resource")
        if r.status_code == 200:
            servers = r.json().get("authorization_servers") or []
            if servers:
                as_url = str(servers[0])
    except Exception:  # noqa: BLE001 — degrade to probing the origin
        pass

    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            r = await client.get(f"{_origin(as_url)}{path}")
            if r.status_code != 200:
                continue
            m = r.json()
            auth = m.get("authorization_endpoint")
            token = m.get("token_endpoint")
            if auth and token:
                return OAuthEndpoints(
                    authorization_endpoint=str(auth),
                    token_endpoint=str(token),
                    registration_endpoint=str(m.get("registration_endpoint", "")),
                    scopes=list(m.get("scopes_supported") or []),
                )
        except Exception:  # noqa: BLE001
            continue
    return None


# ── dynamic client registration (RFC 7591) ───────────────────────────────────


async def register_client(client: Any, registration_endpoint: str, redirect_uri: str, name: str = "DevAI") -> dict[str, str]:
    """Register a public client; returns {client_id, client_secret?}. Raises on
    failure so the caller can fall back to a catalog-provided client."""
    body = {
        "client_name": name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    r = await client.post(registration_endpoint, json=body)
    r.raise_for_status()
    data = r.json()
    return {"client_id": str(data.get("client_id", "")), "client_secret": str(data.get("client_secret", ""))}


# ── authorize / token ────────────────────────────────────────────────────────


def authorize_url(
    ep: OAuthEndpoints,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: list[str] | None = None,
    resource: str = "",
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    scope = " ".join(scopes or ep.scopes or [])
    if scope:
        params["scope"] = scope
    if resource:  # RFC 8707 — bind the token to this MCP server
        params["resource"] = resource
    return f"{ep.authorization_endpoint}?{urlencode(params)}"


@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: str = ""

    @classmethod
    def from_response(cls, data: dict[str, Any], *, now: float) -> TokenSet:
        ttl = float(data.get("expires_in") or 3600)
        return cls(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=now + ttl - 60,  # refresh a minute early
            token_type=str(data.get("token_type", "Bearer")),
            scope=str(data.get("scope", "")),
        )

    def expired(self, *, now: float) -> bool:
        return not self.access_token or (self.expires_at and now >= self.expires_at)


async def exchange_code(
    client: Any,
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    client_secret: str = "",
    resource: str = "",
    now: float,
) -> TokenSet:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    if resource:
        form["resource"] = resource
    r = await client.post(token_endpoint, data=form, headers={"Accept": "application/json"})
    r.raise_for_status()
    return TokenSet.from_response(r.json(), now=now)


async def refresh_access(
    client: Any,
    token_endpoint: str,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str = "",
    now: float,
) -> TokenSet:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    r = await client.post(token_endpoint, data=form, headers={"Accept": "application/json"})
    r.raise_for_status()
    ts = TokenSet.from_response(r.json(), now=now)
    if not ts.refresh_token:  # some servers don't rotate; keep the old one
        ts.refresh_token = refresh_token
    return ts


# ── on-demand access token (federation side) ─────────────────────────────────

# Per-pod access-token cache, keyed by refresh-token hash. Access tokens are
# short-lived and re-mintable, so caching them in memory (not SM) is fine — the
# durable secret is the refresh token, which lives in the user's GCP SM.
_TOKEN_CACHE: dict[str, TokenSet] = {}


def _cache_key(refresh_token: str, token_endpoint: str) -> str:
    return hashlib.sha256(f"{token_endpoint}|{refresh_token}".encode()).hexdigest()[:24]


async def access_token(
    *,
    refresh_token: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str = "",
    now: float,
) -> str:
    """A valid Bearer access token for an oauth MCP connector.

    Mints from the stored refresh token, caches per pod until just before
    expiry, and refreshes transparently. Returns "" on failure (the leg then
    drops with a clear log rather than sending a bad token).
    """
    if not (refresh_token and token_endpoint and client_id):
        return ""
    key = _cache_key(refresh_token, token_endpoint)
    cached = _TOKEN_CACHE.get(key)
    if cached and not cached.expired(now=now):
        return cached.access_token
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            ts = await refresh_access(
                client, token_endpoint, refresh_token=refresh_token,
                client_id=client_id, client_secret=client_secret, now=now,
            )
    except Exception:  # noqa: BLE001
        logger.warning("mcp_oauth: token refresh failed for %s", token_endpoint, exc_info=True)
        return ""
    _TOKEN_CACHE[key] = ts
    return ts.access_token


__all__ = [
    "OAuthEndpoints",
    "TokenSet",
    "access_token",
    "authorize_url",
    "discover_endpoints",
    "exchange_code",
    "pkce_pair",
    "random_state",
    "refresh_access",
    "register_client",
]
