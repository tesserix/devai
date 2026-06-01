"""Local username/password identity backend — LOCAL/sandbox ONLY.

Two sources, in priority order:

1. **Database** (the real local deploy): a ``devai_local_users`` table seeded
   by the devai-api chart's ``auth-seed`` init container (gated, local only).
   This is the "db auth" setup — credentials live in Postgres, provisioned by
   an init step, never in prod.
2. **In-memory roster** (tests / no-DB fallback): a list of user dicts.

NEVER enabled in production — prod terminates auth at the auth-bff and selects
the ``noop`` backend. Passwords are compared in constant time; an empty source
fails every login closed.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from devai.adapters.identity.base import AuthResult, IdentityAdapter

logger = logging.getLogger(__name__)


class LocalDBIdentityAdapter(IdentityAdapter):
    provider = "local_db"

    def __init__(self, pool: Any | None = None, users: list[dict] | None = None) -> None:
        # When a Postgres pool is present we authenticate against the seeded
        # table; otherwise we use the in-memory roster (tests / env fallback).
        self._pool = pool
        self._users: dict[str, dict] = {}
        for u in users or []:
            name = str(u.get("username", "")).strip()
            if name and u.get("password"):
                self._users[name] = u
        if pool is None and not self._users:
            logger.warning(
                "local_db identity: no DB pool and no users — all logins fail. "
                "Seed devai_local_users (auth-seed init) or set DEVAI_LOCAL_AUTH_USERS."
            )

    def login_config(self) -> dict:
        cfg: dict[str, Any] = {"mode": "local_db"}
        if self._pool is None:
            cfg["usernames"] = sorted(self._users.keys())
        return cfg

    async def authenticate(self, username: str, password: str) -> AuthResult | None:
        username = (username or "").strip()
        if not username:
            return None
        record = await self._lookup(username)
        if not record:
            return None
        expected = str(record.get("password", ""))
        if not expected or not hmac.compare_digest(expected, password or ""):
            return None
        return AuthResult(
            login=username,
            email=str(record.get("email") or f"{username}@devai.local"),
            name=str(record.get("name") or username.capitalize()),
            roles=list(record.get("roles") or []),
        )

    async def _lookup(self, username: str) -> dict | None:
        """Fetch a user record from the DB (preferred) or the in-memory roster."""
        if self._pool is not None:
            try:
                row = await self._pool.fetchrow(
                    "SELECT username, password, email, name, roles FROM devai_local_users WHERE username = $1",
                    username,
                )
            except Exception:  # noqa: BLE001 — DB hiccup never crashes login
                logger.exception("local_db: devai_local_users lookup failed")
                return None
            if row is None:
                return None
            roles = row["roles"]
            if isinstance(roles, str):  # asyncpg returns jsonb as text
                try:
                    roles = json.loads(roles)
                except json.JSONDecodeError:
                    roles = []
            return {
                "password": row["password"],
                "email": row["email"],
                "name": row["name"],
                "roles": roles or [],
            }
        return self._users.get(username)


__all__ = ["LocalDBIdentityAdapter"]
