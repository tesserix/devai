"""OAuth 2.1 client-credentials token provider for registry access.

This is the SECURE way a tool connects to the registry: instead of holding a
long-lived static token, the tool authenticates as an OIDC client (its
``client_id`` + ``client_secret``, sourced from a secret manager) and exchanges
them at the IdP's token endpoint for a short-lived JWT carrying scopes
(``registry:read`` / ``registry:write``). The registry verifies that JWT via
JWKS — it never sees the client secret and issues nothing itself.

The token is cached and refreshed shortly before expiry, so a long-running
process keeps a valid short-lived credential without re-reading the secret.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class ClientCredentialsTokenProvider:
    """Fetches and caches a bearer token via the client-credentials grant."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: str = "registry:read registry:write",
        audience: str = "",
        leeway_seconds: int = 30,
        timeout: float = 10.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._audience = audience
        self._leeway = leeway_seconds
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def token(self) -> str:
        """Return a valid token, refreshing if missing or near expiry."""
        with self._lock:
            if self._token and time.monotonic() < self._expires_at - self._leeway:
                return self._token
            self._refresh_locked()
            return self._token

    def _refresh_locked(self) -> None:
        import httpx

        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scopes,
        }
        if self._audience:
            data["audience"] = self._audience
        try:
            r = httpx.post(
                self._token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
            r.raise_for_status()
            body: dict[str, Any] = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("registry oidc token fetch failed: %s", e)
            # Keep any previously-cached token; do not raise into the adapter.
            return
        self._token = body.get("access_token", "") or ""
        expires_in = float(body.get("expires_in", 300) or 300)
        self._expires_at = time.monotonic() + expires_in


def resolve_registry_token(settings: Any) -> str:
    """Resolve the bearer token a registry client should send.

    Order of preference (most → least secure):
      1. OIDC client-credentials (``registry_oidc_token_url`` + ``registry_client_id``
         + ``registry_client_secret``) → short-lived scoped JWT.
      2. A static ``registry_token`` (e.g. from a secret manager) — simpler but
         long-lived.
      3. Empty string — anonymous (local dev / public reads only).
    """
    token_url = getattr(settings, "registry_oidc_token_url", "") or ""
    client_id = getattr(settings, "registry_client_id", "") or ""
    client_secret = getattr(settings, "registry_client_secret", "") or ""
    if token_url and client_id and client_secret:
        scopes = getattr(settings, "registry_scopes", "registry:read registry:write") or ""
        audience = getattr(settings, "registry_audience", "") or ""
        provider = ClientCredentialsTokenProvider(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            audience=audience,
        )
        return provider.token()
    return getattr(settings, "registry_token", "") or ""


__all__ = ["ClientCredentialsTokenProvider", "resolve_registry_token"]
