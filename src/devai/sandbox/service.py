"""SandboxService — create / inspect / destroy / reap sandboxes.

Ownership is enforced here, not at the route: a foreign sandbox reads as absent so
the API cannot leak its existence. The TTL reaper mirrors ``preview/service.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from devai.kit.versions import AdkVersionCatalogue, UnknownAdkVersion
from devai.sandbox.models import AgentRef, ImportSnapshot, SandboxRecord, SandboxSpec, SandboxStatus, ToolMode
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
        runtime: Any | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._settings = settings
        self._provisioner = provisioner
        self._adk_catalogue = adk_catalogue
        self._runtime = runtime
        self._reaper_task: asyncio.Task[None] | None = None
        # Namespaces seen Terminating on the previous sweep — the reaper
        # re-deletes only what Kubernetes GC failed to clear on its own.
        self._terminating_seen: set[str] = set()

    async def create(
        self,
        spec: SandboxSpec,
        *,
        owner: str,
        tenant_id: str = "",
        user_id: str = "",
        sandbox_id: str | None = None,
    ) -> SandboxRecord:
        if not owner:
            raise SandboxError("a sandbox needs an owner")
        if spec.import_snapshot is not None:
            raise SandboxError("import_snapshot is server-managed")
        if sandbox_id:
            existing = await self.get(sandbox_id, owner=owner, is_admin=False)
            if existing is not None:
                if not _same_sandbox_request(existing.spec, spec):
                    raise SandboxError("idempotency key was already used for a different sandbox request")
                if existing.status in {SandboxStatus.DESTROYING, SandboxStatus.DESTROYED}:
                    raise SandboxError("idempotent sandbox request already reached a terminal cleanup state")
                if existing.status is SandboxStatus.READY or self._provisioner is None:
                    return existing
                return await self._provision(existing)
        spec = await self._materialize_import(spec, owner_scope=tenant_id or owner)
        self._assert_refs_published(spec)
        spec = await self._inline_draft_prompt_ref(spec)
        spec = await self._pin_adk_version(spec)
        if spec.agent is None:  # guarded by SandboxSpec and import hydration
            raise SandboxError("sandbox agent is unresolved")

        now = datetime.now(UTC)
        record = SandboxRecord(
            id=sandbox_id or str(uuid.uuid4()),
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
            from devai.adapters.telemetry import get_global_telemetry

            quota = "monthly_cost" if "monthly" in str(exc).lower() else "concurrent"
            get_global_telemetry().incr("devai.sandbox.quota_rejections", attrs={"quota": quota})
            raise SandboxError(str(exc)) from exc
        logger.info("sandbox %s created for %s (agent=%s@%s)", record.id, owner, spec.agent.name, spec.agent.version)
        return await self._provision(record)

    async def _inline_draft_prompt_ref(self, spec: SandboxSpec) -> SandboxSpec:
        """Resolve a promptRef-only draft into an inline system prompt.

        Published agents get promptRef composed by the registry's resolver; a
        draft never reaches it, so without this the sandbox runs the agent with
        an empty system prompt and every result tests the wrong configuration.
        """
        draft = spec.draft
        dspec = draft.get("spec") if isinstance(draft, dict) else None
        if not isinstance(dspec, dict) or self._registry is None:
            return spec
        if str(dspec.get("systemPrompt") or "").strip():
            return spec
        prompts = dspec.get("prompts") if isinstance(dspec.get("prompts"), list) else []
        ref = str(dspec.get("promptRef") or "").strip() or str((prompts or [""])[0] or "").strip()
        if not ref:
            return spec
        try:
            envelope = await asyncio.to_thread(self._registry.get_artifact_envelope, "prompts", ref)
        except Exception:  # noqa: BLE001 — registry trouble degrades, never blocks
            logger.warning("sandbox: prompt lookup for draft promptRef %r failed", ref, exc_info=True)
            return spec
        text = str(((envelope or {}).get("spec") or {}).get("systemPrompt") or "")
        if not text.strip():
            raise SandboxError(f"draft promptRef {ref!r} has no non-empty spec.systemPrompt")
        new_draft = copy.deepcopy(draft)
        new_draft["spec"]["systemPrompt"] = text
        return spec.model_copy(update={"draft": new_draft})

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
            row = await self._db.get_sandbox(sandbox_id)
            if row is None:
                return
            await self._db.touch_sandbox(
                sandbox_id,
                SandboxSpec.model_validate(_jsonb(row["spec"])).ttl_seconds,
            )

    async def reap_expired(self) -> int:
        rows = await self._db.expired_sandboxes(datetime.now(UTC))
        for row in rows:
            await self._teardown(_to_record(row))
            await self._db.set_sandbox_status(row["id"], SandboxStatus.DESTROYED.value)
        return len(rows)

    async def reap_orphan_namespaces(self) -> int:
        """Delete devai-sbx-* namespaces whose sandbox row is gone or expired.

        The namespace is the boundary, so an orphan is a whole leaked sandbox;
        this sweep is what makes crash-during-teardown safe.
        """
        if self._runtime is None:
            return 0
        reaped = 0
        namespaces = await self._runtime.list_namespaces(
            label_selector="app.kubernetes.io/managed-by=devai,devai.tesserix.app/sandbox"
        )
        for ns in namespaces:
            name = str(ns.get("metadata", {}).get("name", ""))
            sid = str((ns.get("metadata", {}).get("labels") or {}).get("devai.tesserix.app/sandbox") or "")
            row = await self._db.get_sandbox(sid) if sid else None
            if row is not None:
                live = str(row.get("status")) not in ("destroyed", "failed")
                expired = row.get("expires_at") is not None and row["expires_at"] <= datetime.now(UTC)
                if live and not expired:
                    continue
            phase = str((ns.get("status") or {}).get("phase") or "")
            if phase == "Terminating" and name not in self._terminating_seen:
                self._terminating_seen.add(name)
                continue
            if phase == "Terminating":
                logger.warning("sandbox reaper: namespace %s stuck Terminating — re-deleting", name)
            with contextlib.suppress(Exception):
                await self._runtime.delete_namespace(name)
                reaped += 1
            self._terminating_seen.discard(name)
        return reaped

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
                with contextlib.suppress(Exception):
                    orphans = await self.reap_orphan_namespaces()
                    if orphans:
                        logger.info("sandbox: reaped %d orphan namespace(s)", orphans)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a reap failure must not kill the loop
                logger.warning("sandbox: reap failed", exc_info=True)
            await asyncio.sleep(_REAPER_MAX_INTERVAL_SECONDS)

    # ── reference validation ──────────────────────────────────────────

    async def _materialize_import(self, spec: SandboxSpec, *, owner_scope: str) -> SandboxSpec:
        if spec.import_id is None:
            return spec
        try:
            imported = await self._db.get_agent_import(owner_scope, spec.import_id)
        except Exception as exc:  # noqa: BLE001
            raise SandboxError("agent import storage unavailable") from exc
        if imported is None:
            raise SandboxError(f"agent import {spec.import_id} not found")
        if imported.get("state") != "ready":
            raise SandboxError(f"agent import {spec.import_id} is not ready")
        conformance = imported.get("conformance") or {}
        if conformance.get("level") not in {"callable", "sandbox_runnable", "verified"}:
            raise SandboxError(f"agent import {spec.import_id} is not callable")
        agent = imported.get("agent") or {}
        exact_agent = AgentRef(
            name=str(agent.get("name") or ""),
            version=str(agent.get("version") or ""),
        )
        if spec.agent is not None and spec.agent != exact_agent:
            raise SandboxError("sandbox agent override does not match the immutable import")
        permissions = imported.get("permissions") or {}
        if not isinstance(permissions, dict):
            raise SandboxError("agent import permissions are invalid")
        _assert_permissions_not_widened(spec, permissions)
        snapshot = ImportSnapshot(
            import_id=spec.import_id,
            registry_ref=str(imported.get("registry_ref") or ""),
            agent_digest=str(agent.get("digest") or ""),
            dependency_lock=list(imported.get("dependency_lock") or []),
            runtime=dict(agent.get("runtime") or {}),
            permissions=permissions,
        )
        return spec.model_copy(update={"agent": exact_agent, "import_snapshot": snapshot})

    def _assert_refs_published(self, spec: SandboxSpec) -> None:
        """Refuse a spec pointing at an artifact that isn't published.

        `artifact_exists` returns None when the registry can't answer; an
        unreachable registry must not block sandbox creation.
        """
        if self._registry is None or spec.import_snapshot is not None:
            return
        # A draft is unpublished by definition — that is what it is being tested for.
        if spec.agent is None:
            raise SandboxError("sandbox agent is unresolved")
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


def _assert_permissions_not_widened(spec: SandboxSpec, permissions: dict[str, Any]) -> None:
    network = permissions.get("network") or {}
    if not isinstance(network, dict):
        raise SandboxError("agent import network permissions are invalid")
    allowed_domains = set(network.get("domains") or [])
    if not set(spec.allow_domains).issubset(allowed_domains):
        raise SandboxError("sandbox network domains exceed the imported permission lock")

    allowed_scopes = set(permissions.get("scopes") or [])
    if not set(spec.allow_scopes).issubset(allowed_scopes):
        raise SandboxError("sandbox credential scopes exceed the imported permission lock")
    for field in ("workspace", "browser", "ide"):
        if getattr(spec, field) and permissions.get(field) is not True:
            raise SandboxError(f"sandbox {field} exceeds the imported permission lock")

    tool_permissions = permissions.get("tools") or {}
    if not isinstance(tool_permissions, dict):
        raise SandboxError("agent import tool permissions are invalid")
    real_tools = set(tool_permissions.get("real") or [])
    if spec.tools.default_mode is ToolMode.REAL and "*" not in real_tools:
        raise SandboxError("sandbox default real tool mode exceeds the imported permission lock")
    requested_real = {name for name, mode in spec.tools.overrides.items() if mode is ToolMode.REAL}
    if "*" not in real_tools and not requested_real.issubset(real_tools):
        raise SandboxError("sandbox real tools exceed the imported permission lock")


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


def _same_sandbox_request(stored: SandboxSpec, requested: SandboxSpec) -> bool:
    stored_body = stored.model_dump(mode="json")
    requested_body = requested.model_dump(mode="json")
    if requested.import_snapshot is None:
        stored_body["import_snapshot"] = None
    if requested.agent is None:
        stored_body["agent"] = None
    if requested.adk_version is None:
        stored_body["adk_version"] = None
    return stored_body == requested_body


__all__ = ["SandboxError", "SandboxService"]
