"""No-op identity backend — the production / default selection.

In prod, auth is terminated by the auth-bff (GIP/Keycloak) and the browser
arrives with a session already minted, so this adapter is never asked to
authenticate. It exists so the factory always returns *something* and the
``local_db`` path is the only one that actually checks passwords.
"""

from __future__ import annotations

from devai.adapters.identity.base import AuthResult, IdentityAdapter


class NoopIdentityAdapter(IdentityAdapter):
    provider = "noop"

    def login_config(self) -> dict:
        # gip: the login page renders the Google/GIP button and talks to the
        # auth-bff directly (unchanged production behaviour).
        return {"mode": "gip"}

    async def authenticate(self, username: str, password: str) -> AuthResult | None:
        return None


__all__ = ["NoopIdentityAdapter"]
