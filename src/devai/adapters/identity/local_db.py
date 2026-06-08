"""Local username/password identity backend — LOCAL/sandbox ONLY.

Two sources, in priority order:

1. **Database** (the real local deploy): a ``devai_local_users`` table seeded
   by the devai-api chart's ``auth-seed`` init container (gated, local only).
   This is the "db auth" setup — credentials live in Postgres, provisioned by
   an init step, never in prod.
2. **In-memory roster** (tests / no-DB fallback): a list of user dicts.

NEVER enabled in production — prod terminates auth at the auth-bff and selects
the ``noop`` backend.

**Password storage.** The seeder writes a passlib hash (bcrypt by default,
argon2 if available) into ``devai_local_users.password``; ``authenticate``
verifies the supplied password against that hash via passlib (constant-time
inside the scheme). For back-compat — the in-memory roster and older seeds
still carry plaintext — a stored value that isn't a recognized passlib hash
is compared as plaintext in constant time. An empty source, an empty stored
value, or a hash we can't verify (passlib missing) all fail the login closed.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from devai.adapters.identity.base import AuthResult, IdentityAdapter

logger = logging.getLogger(__name__)

# passlib schemes we accept for stored hashes. bcrypt is the default the seeder
# writes; argon2 is preferred when its backend is installed. Order matters:
# the first scheme is used by ``hash_password``.
_PASSLIB_SCHEMES = ("argon2", "bcrypt")

# Prefixes a stored value carries when it's a passlib hash (Modular Crypt
# Format). Used to decide hash-verify vs plaintext-compat without importing
# passlib just to inspect a string.
_HASH_PREFIXES = ("$argon2", "$2a$", "$2b$", "$2y$", "$bcrypt", "$bcrypt-sha256$")

_pwd_context: Any | None = None
_pwd_context_loaded = False


def _password_context() -> Any | None:
    """Lazily build (and cache) the passlib CryptContext, or None if passlib
    isn't installed. Mirrors the adapter family's lazy-SDK rule — a deployment
    that never uses local_db never imports passlib."""
    global _pwd_context, _pwd_context_loaded
    if _pwd_context_loaded:
        return _pwd_context
    _pwd_context_loaded = True
    try:
        from passlib.context import CryptContext  # lazy import

        # Only register schemes whose backend actually imports, so a missing
        # argon2 backend doesn't make the whole context unusable for bcrypt.
        schemes: list[str] = []
        for scheme in _PASSLIB_SCHEMES:
            try:
                CryptContext(schemes=[scheme])  # probes the backend
                schemes.append(scheme)
            except Exception:  # noqa: BLE001 — backend missing; skip this scheme
                continue
        _pwd_context = CryptContext(schemes=schemes, deprecated="auto") if schemes else None
    except Exception:  # noqa: BLE001 — passlib not installed
        logger.debug("local_db identity: passlib unavailable; hashed logins disabled", exc_info=True)
        _pwd_context = None
    return _pwd_context


def _looks_hashed(value: str) -> bool:
    return value.startswith(_HASH_PREFIXES)


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage (used by the auth-seed init step).

    Raises ``RuntimeError`` when passlib isn't installed — the seeder must fail
    loudly rather than persist a plaintext password by accident."""
    ctx = _password_context()
    if ctx is None:
        raise RuntimeError("passlib is required to hash local_db passwords; install passlib[bcrypt]")
    return ctx.hash(password)


def _verify_password(supplied: str, stored: str) -> bool:
    """Constant-time verify ``supplied`` against the ``stored`` credential.

    A passlib-hashed ``stored`` is verified via passlib (fails closed if
    passlib is unavailable). A non-hashed ``stored`` is compared as plaintext
    in constant time (back-compat for the in-memory roster / legacy seeds)."""
    if not stored:
        return False
    if _looks_hashed(stored):
        ctx = _password_context()
        if ctx is None:
            logger.warning("local_db identity: stored hash present but passlib unavailable — failing login closed")
            return False
        try:
            return bool(ctx.verify(supplied or "", stored))
        except Exception:  # noqa: BLE001 — malformed hash / unknown scheme → reject
            logger.debug("local_db identity: hash verification failed", exc_info=True)
            return False
    return hmac.compare_digest(stored, supplied or "")


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
        if not _verify_password(password or "", expected):
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


__all__ = ["LocalDBIdentityAdapter", "hash_password"]
