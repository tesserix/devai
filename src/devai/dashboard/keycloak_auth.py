"""Keycloak OIDC authentication for the DevAI dashboard."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from devai.config import Settings

logger = logging.getLogger(__name__)


class KeycloakOIDC:
    """Handles Keycloak OIDC authentication flow for the dashboard."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=15.0)
        self._well_known: dict[str, Any] | None = None

    @property
    def issuer_url(self) -> str:
        """Keycloak issuer URL for the internal realm."""
        return f"{self.config.keycloak_url}/realms/{self.config.keycloak_realm}"

    @property
    def well_known_url(self) -> str:
        return f"{self.issuer_url}/.well-known/openid-configuration"

    async def get_well_known(self) -> dict[str, Any]:
        """Fetch and cache the OIDC well-known configuration."""
        if self._well_known is None:
            resp = await self._http.get(self.well_known_url)
            resp.raise_for_status()
            self._well_known = resp.json()
        return self._well_known

    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        """Generate the Keycloak authorization URL."""
        params = {
            "client_id": self.config.keycloak_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.issuer_url}/protocol/openid-connect/auth?{query}"

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens."""
        resp = await self._http.post(
            f"{self.issuer_url}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.config.keycloak_client_id,
                "client_secret": self.config.keycloak_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_userinfo(self, access_token: str) -> dict[str, Any]:
        """Get user info from the Keycloak userinfo endpoint."""
        resp = await self._http.get(
            f"{self.issuer_url}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an access token."""
        resp = await self._http.post(
            f"{self.issuer_url}/protocol/openid-connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.config.keycloak_client_id,
                "client_secret": self.config.keycloak_client_secret,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def validate_token(self, access_token: str) -> dict[str, Any] | None:
        """Validate a token via the introspection endpoint."""
        resp = await self._http.post(
            f"{self.issuer_url}/protocol/openid-connect/token/introspect",
            data={
                "client_id": self.config.keycloak_client_id,
                "client_secret": self.config.keycloak_client_secret,
                "token": access_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("active"):
            return data
        return None

    async def logout(self, refresh_token: str) -> None:
        """Logout from Keycloak (invalidate the refresh token)."""
        await self._http.post(
            f"{self.issuer_url}/protocol/openid-connect/logout",
            data={
                "client_id": self.config.keycloak_client_id,
                "client_secret": self.config.keycloak_client_secret,
                "refresh_token": refresh_token,
            },
        )

    async def close(self) -> None:
        await self._http.aclose()
