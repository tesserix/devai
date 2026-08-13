"""Drive a sandbox through pending → provisioning → ready → destroyed.

The provisioner owns the cluster-side objects only; the Job itself is dispatched
later by the normal runner path with `apply_sandbox_boundary` on top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from devai.sandbox.isolation import build_isolation_manifests
from devai.sandbox.models import SandboxStatus

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)


class _StatusStore(Protocol):
    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None: ...


class SandboxProvisioner:
    def __init__(self, runtime: Any, store: _StatusStore) -> None:
        self._runtime = runtime
        self._store = store

    async def provision(self, record: SandboxRecord) -> SandboxRecord:
        await self._set(record, SandboxStatus.PROVISIONING)
        namespace = getattr(getattr(self._runtime, "config", None), "namespace", "devai")
        try:
            await self._runtime.connect()
            for manifest in build_isolation_manifests(record, namespace=namespace):
                await self._runtime.apply_manifest(manifest)
        except Exception as e:  # noqa: BLE001 — a cluster failure is a sandbox outcome, not a crash
            logger.warning("sandbox %s: provisioning failed", record.id, exc_info=True)
            return await self._set(record, SandboxStatus.FAILED, {"error": str(e)})
        return await self._set(record, SandboxStatus.READY)

    async def teardown(self, record: SandboxRecord) -> SandboxRecord:
        await self._set(record, SandboxStatus.DESTROYING)
        namespace = getattr(getattr(self._runtime, "config", None), "namespace", "devai")
        for manifest in build_isolation_manifests(record, namespace=namespace):
            try:
                await self._runtime.delete_manifest(manifest["kind"], manifest["metadata"]["name"], namespace)
            except Exception:  # noqa: BLE001 — already gone is the desired end state
                logger.debug("sandbox %s: %s already absent", record.id, manifest["kind"])
        return await self._set(record, SandboxStatus.DESTROYED)

    async def _set(
        self, record: SandboxRecord, status: SandboxStatus, detail: dict[str, Any] | None = None
    ) -> SandboxRecord:
        await self._store.set_sandbox_status(record.id, status.value, detail)
        return record.model_copy(update={"status": status, "detail": detail or record.detail})


__all__ = ["SandboxProvisioner"]
