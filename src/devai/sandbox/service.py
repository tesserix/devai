"""SandboxService — create / inspect / destroy / reap sandboxes.

Ownership is enforced here, not at the route: a foreign sandbox reads as absent so
the API cannot leak its existence. The TTL reaper mirrors ``preview/service.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus

if TYPE_CHECKING:
    from devai.services.database import Database

logger = logging.getLogger(__name__)

_REAPER_MAX_INTERVAL_SECONDS = 5 * 60


class SandboxError(ValueError):
    """Invalid sandbox input, a foreign sandbox, or an unavailable runtime."""


class SandboxService:
    def __init__(self, db: Database | Any, *, registry: Any | None = None, settings: Any | None = None) -> None:
        self._db = db
        self._registry = registry
        self._settings = settings
        self._reaper_task: asyncio.Task[None] | None = None

    async def create(self, spec: SandboxSpec, *, owner: str) -> SandboxRecord:
        if not owner:
            raise SandboxError("a sandbox needs an owner")
        self._assert_refs_published(spec)

        now = datetime.now(UTC)
        record = SandboxRecord(
            id=str(uuid.uuid4()),
            owner=owner,
            spec=spec,
            status=SandboxStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(seconds=spec.ttl_seconds),
            last_access_at=now,
        )
        await self._db.create_sandbox(
            sandbox_id=record.id,
            owner=owner,
            spec=spec.model_dump(mode="json"),
            status=record.status.value,
            created_at=record.created_at,
            expires_at=record.expires_at,
            last_access_at=record.last_access_at,
        )
        logger.info("sandbox %s created for %s (agent=%s@%s)", record.id, owner, spec.agent.name, spec.agent.version)
        return record

    async def get(self, sandbox_id: str, *, owner: str, is_admin: bool = False) -> SandboxRecord | None:
        row = await self._db.get_sandbox(sandbox_id)
        if row is None:
            return None
        record = _to_record(row)
        if not is_admin and record.owner != owner:
            return None  # absent, not forbidden — don't leak existence
        return record

    async def list(self, *, owner: str, is_admin: bool = False, limit: int = 100) -> list[SandboxRecord]:
        rows = await self._db.list_sandboxes(None if is_admin else owner, limit)
        return [_to_record(r) for r in rows]

    async def destroy(self, sandbox_id: str, *, owner: str, is_admin: bool = False) -> None:
        record = await self.get(sandbox_id, owner=owner, is_admin=is_admin)
        if record is None:
            raise SandboxError(f"sandbox {sandbox_id} not found")
        await self._db.set_sandbox_status(sandbox_id, SandboxStatus.DESTROYED.value)

    async def touch(self, sandbox_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._db.touch_sandbox(sandbox_id)

    async def reap_expired(self) -> int:
        rows = await self._db.expired_sandboxes(datetime.now(UTC))
        for row in rows:
            await self._db.set_sandbox_status(row["id"], SandboxStatus.DESTROYED.value)
        return len(rows)

    # ── TTL reaper ────────────────────────────────────────────────────

    def start_reaper(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop_reaper(self) -> None:
        if self._reaper_task is None:
            return
        self._reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reaper_task
        self._reaper_task = None

    async def _reaper_loop(self) -> None:
        while True:
            try:
                reaped = await self.reap_expired()
                if reaped:
                    logger.info("sandbox: reaped %d expired sandbox(es)", reaped)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a reap failure must not kill the loop
                logger.warning("sandbox: reap failed", exc_info=True)
            await asyncio.sleep(_REAPER_MAX_INTERVAL_SECONDS)

    # ── reference validation ──────────────────────────────────────────

    def _assert_refs_published(self, spec: SandboxSpec) -> None:
        """Refuse a spec pointing at an artifact that isn't published.

        `artifact_exists` returns None when the registry can't answer; an
        unreachable registry must not block sandbox creation.
        """
        if self._registry is None:
            return
        refs = [("agents", spec.agent.name)]
        if spec.prompt is not None:
            refs.append(("prompts", spec.prompt.ref))
        for plural, name in refs:
            try:
                exists = self._registry.artifact_exists(plural, name)
            except Exception:  # noqa: BLE001 — registry trouble degrades, never blocks
                logger.warning("sandbox: registry lookup for %s/%s failed", plural, name, exc_info=True)
                continue
            if exists is False:
                raise SandboxError(f"{plural[:-1]} {name} is not published")


def _to_record(row: dict[str, Any]) -> SandboxRecord:
    spec = row["spec"]
    if isinstance(spec, str):  # asyncpg hands JSONB back as text
        spec = json.loads(spec)
    return SandboxRecord(
        id=row["id"],
        owner=row["owner"],
        spec=SandboxSpec.model_validate(spec),
        status=SandboxStatus(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_access_at=row.get("last_access_at"),
        detail=row.get("detail") or {},
    )


__all__ = ["SandboxError", "SandboxService"]
