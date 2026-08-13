"""Short-lived scoped credentials for sandboxes — never a standing secret.

The boundary work (#180) stops a sandbox mounting production secrets. This is
the other half: what happens when the agent legitimately needs one. The control
plane holds every secret; the sandbox asks for the narrowest credential that
satisfies the request and gets one that expires inside its own lifetime.

Nothing here falls back to a platform key. A missing source is reported to the
caller, because a silent fallback is exactly the standing secret this exists to
remove.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)

# Short enough that a leaked credential outlives nothing worth having.
MAX_GRANT_TTL_S = 900


class BrokerError(Exception):
    """The broker cannot satisfy a request, and says so rather than guessing."""


@runtime_checkable
class CredentialSource(Protocol):
    """Mints one kind of credential, scoped as narrowly as the backend allows."""

    kind: str

    async def mint(self, scope: str, *, ttl_seconds: int) -> tuple[str, datetime]: ...

    async def revoke(self, secret: str) -> None: ...


class Grant(BaseModel):
    """The audit answer to "what could this sandbox reach, and until when".

    Deliberately holds no secret: it is written to the audit log and read back
    by anyone who can read a sandbox.
    """

    id: str
    sandbox_id: str
    kind: str
    scope: str
    granted_to: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CredentialBroker:
    def __init__(
        self,
        sources: dict[str, CredentialSource],
        *,
        audit: Callable[[Grant], Awaitable[None]] | None = None,
    ) -> None:
        self._sources = sources
        self._audit = audit
        self._grants: dict[str, list[tuple[Grant, str]]] = {}

    async def grant(
        self,
        record: SandboxRecord,
        *,
        kind: str,
        scope: str,
        ttl_seconds: int = MAX_GRANT_TTL_S,
    ) -> tuple[Grant, str]:
        if scope not in record.spec.allow_scopes:
            raise BrokerError(
                f"sandbox {record.id} was not created with access to {scope!r}; "
                "a run can only be granted a scope its spec declares"
            )
        source = self._sources.get(kind)
        if source is None:
            raise BrokerError(f"no credential source for {kind!r}; this sandbox has no {kind} credential")

        ttl = min(ttl_seconds, MAX_GRANT_TTL_S, self._remaining_life(record))
        if ttl <= 0:
            raise BrokerError("sandbox has expired; refusing to mint a credential for it")

        secret, expires_at = await source.mint(scope, ttl_seconds=ttl)
        now = datetime.now(UTC)
        entry = Grant(
            id=f"grant-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            kind=kind,
            scope=scope,
            granted_to=record.owner,
            created_at=now,
            expires_at=expires_at,
        )
        self._grants.setdefault(record.id, []).append((entry, secret))
        await self._record(entry)
        return entry, secret

    def grants(self, sandbox_id: str) -> list[Grant]:
        return [g for g, _ in self._grants.get(sandbox_id, [])]

    async def revoke_all(self, sandbox_id: str) -> None:
        for entry, secret in self._grants.pop(sandbox_id, []):
            source = self._sources.get(entry.kind)
            if source is not None:
                try:
                    await source.revoke(secret)
                except Exception:  # noqa: BLE001 — an unrevokable grant still expires on its own
                    logger.warning("broker: revoking %s failed; it expires at %s", entry.id, entry.expires_at)
            entry.revoked_at = datetime.now(UTC)
            await self._record(entry)

    async def _record(self, entry: Grant) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(entry)
        except Exception:  # noqa: BLE001 — an audit sink outage must not deny a run its credential
            logger.warning("broker: audit sink rejected grant %s", entry.id, exc_info=True)

    @staticmethod
    def _remaining_life(record: SandboxRecord) -> int:
        expires = record.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return int((expires - datetime.now(UTC)) / timedelta(seconds=1))


__all__ = ["MAX_GRANT_TTL_S", "BrokerError", "CredentialBroker", "CredentialSource", "Grant"]
