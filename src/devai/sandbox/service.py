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

from devai.kit.versions import AdkVersionCatalogue, UnknownAdkVersion
from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus
from devai.services.database import SandboxQuotaExceeded

if TYPE_CHECKING:
    from devai.services.database import Database

logger = logging.getLogger(__name__)

_REAPER_MAX_INTERVAL_SECONDS = 5 * 60


class SandboxError(ValueError):
    """Invalid sandbox input, a foreign sandbox, or an unavailable runtime."""


class SandboxService:
    def __init__(
        self,
        db: Database | Any,
        *,
        registry: Any | None = None,
        settings: Any | None = None,
        provisioner: Any | None = None,
        adk_catalogue: AdkVersionCatalogue | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._settings = settings
        self._provisioner = provisioner
        self._adk_catalogue = adk_catalogue
        self._reaper_task: asyncio.Task[None] | None = None

    async def create(
        self,
        spec: SandboxSpec,
        *,
        owner: str,
        tenant_id: str = "",
        user_id: str = "",
    ) -> SandboxRecord:
        if not owner:
            raise SandboxError("a sandbox needs an owner")
        self._assert_refs_published(spec)
        spec = await self._pin_adk_version(spec)

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
        try:
            await self._db.create_sandbox(
                sandbox_id=record.id,
                owner=owner,
                spec=spec.model_dump(mode="json"),
                status=record.status.value,
                created_at=record.created_at,
                expires_at=record.expires_at,
                last_access_at=record.last_access_at,
                tenant_id=tenant_id,
                user_id=user_id or owner,
                max_live_per_tenant=int(getattr(self._settings, "sandbox_max_live_per_tenant", 5) or 0),
                monthly_cost_limit_usd=float(getattr(self._settings, "sandbox_monthly_cost_limit_usd", 100.0) or 0.0),
            )
        except SandboxQuotaExceeded as exc:
            raise SandboxError(str(exc)) from exc
        logger.info("sandbox %s created for %s (agent=%s@%s)", record.id, owner, spec.agent.name, spec.agent.version)
        return await self._provision(record)

    async def _pin_adk_version(self, spec: SandboxSpec) -> SandboxSpec:
        """Resolve the runtime release now, so the stored spec reproduces the run
        even after newer releases land."""
        if self._adk_catalogue is None:
            return spec
        try:
            resolved = await self._adk_catalogue.resolve(spec.adk_version)
        except UnknownAdkVersion as exc:
            raise SandboxError(str(exc)) from exc
        return spec.model_copy(update={"adk_version": resolved})

    async def _provision(self, record: SandboxRecord) -> SandboxRecord:
        """Bring the sandbox up. A cluster failure yields a FAILED sandbox the
        owner can read the reason from, never a lost record."""
        if self._provisioner is None:
            return record
        try:
            provisioned: SandboxRecord = await self._provisioner.provision(record)
            return provisioned
        except Exception as e:  # noqa: BLE001
            logger.warning("sandbox %s: provisioning raised", record.id, exc_info=True)
            with contextlib.suppress(Exception):
                await self._db.set_sandbox_status(record.id, SandboxStatus.FAILED.value, {"error": str(e)})
            return record.model_copy(update={"status": SandboxStatus.FAILED, "detail": {"error": str(e)}})

    async def _teardown(self, record: SandboxRecord) -> None:
        if self._provisioner is None:
            return
        with contextlib.suppress(Exception):
            await self._provisioner.teardown(record)

    async def get(self, sandbox_id: str, *, owner: str, is_admin: bool = False) -> SandboxRecord | None:
        row = await self._db.get_sandbox(sandbox_id)
        if row is None:
            return None
        record = _to_record(row)
        if not is_admin and record.owner != owner:
            return None  # absent, not forbidden — don't leak existence
        return record

    async def health(self) -> dict[str, Any]:
        """Counts for the Service-health board. Raises if the table is unusable."""
        by_status = await self._db.sandbox_counts()
        return {
            "total": sum(by_status.values()),
            "live": sum(n for s, n in by_status.items() if s != SandboxStatus.DESTROYED.value),
            "by_status": by_status,
        }

    async def list(self, *, owner: str, is_admin: bool = False, limit: int = 100) -> list[SandboxRecord]:
        rows = await self._db.list_sandboxes(None if is_admin else owner, limit)
        return [_to_record(r) for r in rows]

    async def destroy(self, sandbox_id: str, *, owner: str, is_admin: bool = False) -> None:
        record = await self.get(sandbox_id, owner=owner, is_admin=is_admin)
        if record is None:
            raise SandboxError(f"sandbox {sandbox_id} not found")
        await self._teardown(record)
        await self._db.set_sandbox_status(sandbox_id, SandboxStatus.DESTROYED.value)

    async def touch(self, sandbox_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._db.touch_sandbox(sandbox_id)

    async def reap_expired(self) -> int:
        rows = await self._db.expired_sandboxes(datetime.now(UTC))
        for row in rows:
            await self._teardown(_to_record(row))
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
        # A draft is unpublished by definition — that is what it is being tested for.
        refs: list[tuple[str, str]] = [] if spec.draft else [("agents", spec.agent.name)]
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


def _jsonb(value: Any) -> Any:
    """asyncpg hands JSONB back as text; every JSONB column needs this."""
    return json.loads(value) if isinstance(value, str) else value


def _to_record(row: dict[str, Any]) -> SandboxRecord:
    return SandboxRecord(
        id=row["id"],
        owner=row["owner"],
        spec=SandboxSpec.model_validate(_jsonb(row["spec"])),
        status=SandboxStatus(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_access_at=row.get("last_access_at"),
        detail=_jsonb(row.get("detail")) or {},
    )


__all__ = ["SandboxError", "SandboxService"]
