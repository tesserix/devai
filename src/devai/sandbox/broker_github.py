"""A credential source that mints repo-scoped GitHub App tokens.

The installation token the SCM client uses covers every repo the app is
installed on. A sandbox gets the narrow version of the same call: one
repository, the two permissions a code change needs, and an upstream delete on
teardown so the credential dies with the sandbox rather than on its own clock.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# What writing code and opening a PR needs. Anything wider is a different tool
# asking for a different grant.
_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

_DEFAULT_TTL_S = 3600


class GitHubScopedSource:
    kind = "scm"

    def __init__(
        self,
        *,
        app_id: int,
        private_key: str,
        installation_id: int,
        http: Any,
        sign: Callable[[], str] | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._http = http
        self._sign = sign or self._jwt

    def _jwt(self) -> str:
        import jwt  # lazy: a PAT-only deployment never imports PyJWT

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + (9 * 60), "iss": str(self._app_id)}
        return str(jwt.encode(payload, self._private_key, algorithm="RS256"))

    async def mint(self, scope: str, *, ttl_seconds: int) -> tuple[str, datetime]:
        repo = scope.split("/")[-1]
        resp = await self._http.post(
            f"/app/installations/{self._installation_id}/access_tokens",
            json={"repositories": [repo], "permissions": dict(_PERMISSIONS)},
            headers={"Authorization": f"Bearer {self._sign()}", "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        data = resp.json()
        # GitHub's own expiry is an hour; the broker's shorter TTL is what binds.
        expires_at = datetime.now(UTC) + timedelta(seconds=min(ttl_seconds, _DEFAULT_TTL_S))
        return str(data["token"]), expires_at

    async def revoke(self, secret: str) -> None:
        await self._http.delete("/installation/token", headers={"Authorization": f"token {secret}"})


__all__ = ["GitHubScopedSource"]
