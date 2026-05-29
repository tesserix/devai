"""GitHub transport + credential adapter.

One place that knows two things, so the rest of ``GitHubSCMClient`` never
has to:

  1. **How to authenticate** — PAT / OAuth tokens are returned verbatim;
     GitHub App auth signs an RS256 JWT, exchanges it for a short-lived
     installation token, caches it, and refreshes ~5 min before expiry
     (single-flight, so a burst of concurrent calls mints exactly one
     token). Both PAT and GitHub App therefore work identically for every
     caller — the only difference is config.

  2. **Where to reach GitHub** — derives both the REST v3 and the GraphQL
     v4 endpoints from a single base URL, so github.com *and* GitHub
     Enterprise Server (``https://ghe.host/api/v3``) both work without the
     GraphQL URL being hard-coded.

The credential layer follows the project's adapter convention: one small
ABC (:class:`GitHubCredential`) with a static-token impl and a GitHub-App
impl. The vendor SDK (``PyJWT``) is imported lazily inside the App impl, so
a PAT-only deployment never touches it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Refresh an App installation token this many seconds before it expires, so
# an in-flight request never races a 401.
_REFRESH_MARGIN_S = 300.0
_DEFAULT_TOKEN_TTL_S = 3600.0


def graphql_url_for(rest_base: str) -> str:
    """Derive the GraphQL endpoint from a REST base URL.

    * ``https://api.github.com``       → ``https://api.github.com/graphql``
    * ``https://ghe.host/api/v3``      → ``https://ghe.host/api/graphql``
    * anything else                    → ``<base>/graphql`` (best effort)
    """
    base = rest_base.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/api/v3")] + "/api/graphql"
    if base in ("https://api.github.com", "http://api.github.com"):
        return "https://api.github.com/graphql"
    return base + "/graphql"


def _parse_expiry(iso: str | None, default: float) -> float:
    """Parse GitHub's ISO-8601 ``expires_at`` into an epoch timestamp."""
    if not iso:
        return default
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return default


class GitHubCredential(ABC):
    """A source of a GitHub token. PAT returns the same value every call;
    GitHub App mints + caches a short-lived installation token."""

    @abstractmethod
    async def token(self) -> str:
        """Return a bearer token valid for the next request."""


class StaticTokenCredential(GitHubCredential):
    """A fixed PAT / OAuth token, returned verbatim."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token


class GitHubAppCredential(GitHubCredential):
    """GitHub App installation auth: JWT → installation token, cached.

    ``http`` is an :class:`httpx.AsyncClient` whose ``base_url`` is the REST
    API root (so the token-exchange POST works against github.com and GHE
    alike). Token minting is single-flight: the first caller mints, the
    rest await the same result.
    """

    def __init__(
        self,
        app_id: int,
        private_key: str,
        installation_id: int,
        http: httpx.AsyncClient,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._http = http
        self._cached: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _jwt(self) -> str:
        import jwt  # lazy: PAT-only deployments never import PyJWT

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + (9 * 60), "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def token(self) -> str:
        now = time.time()
        if self._cached and now < self._expires_at - _REFRESH_MARGIN_S:
            return self._cached
        async with self._lock:
            # Re-check after acquiring the lock — another coroutine may have
            # minted while we waited (single-flight).
            now = time.time()
            if self._cached and now < self._expires_at - _REFRESH_MARGIN_S:
                return self._cached
            app_jwt = self._jwt()
            resp = await self._http.post(
                f"/app/installations/{self._installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._cached = data["token"]
            self._expires_at = _parse_expiry(data.get("expires_at"), now + _DEFAULT_TOKEN_TTL_S)
            logger.info(
                "Minted GitHub App installation token (installation=%s, ttl≈%ds)",
                self._installation_id,
                int(self._expires_at - now),
            )
            return self._cached


class GitHubTransport:
    """Owns the httpx client + credential + endpoint URLs for one GitHub
    target. Exposes auth headers for REST (``token`` scheme) and GraphQL
    (``bearer`` scheme), both backed by the same credential — so PAT and
    GitHub App are interchangeable across both transports.
    """

    def __init__(
        self,
        base_url: str,
        *,
        use_github_app: bool = False,
        token: str = "",
        app_id: int = 0,
        private_key: str = "",
        installation_id: int = 0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.rest_base = base_url.rstrip("/")
        self.graphql_url = graphql_url_for(self.rest_base)
        self.client = http or httpx.AsyncClient(
            base_url=self.rest_base,
            timeout=30.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        self.credential: GitHubCredential = (
            GitHubAppCredential(app_id, private_key, installation_id, self.client)
            if use_github_app
            else StaticTokenCredential(token)
        )

    async def token(self) -> str:
        return await self.credential.token()

    async def auth_headers(self, scheme: str = "token") -> dict[str, Any]:
        """Authorization header for the given scheme. REST uses ``token``,
        GraphQL uses ``bearer`` — both accept installation tokens, classic
        and fine-grained PATs, and OAuth tokens."""
        tok = await self.credential.token()
        return {"Authorization": f"{scheme} {tok}"} if tok else {}

    async def aclose(self) -> None:
        await self.client.aclose()
