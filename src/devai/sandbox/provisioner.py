"""Drive a sandbox through pending → provisioning → ready → destroyed.

The provisioner owns the cluster-side objects only; the Job itself is dispatched
later by the normal runner path with `apply_sandbox_boundary` on top.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from devai.sandbox.egress import build_proxy_manifests, proxy_service_host
from devai.sandbox.isolation import build_isolation_manifests
from devai.sandbox.job import build_sandbox_secret
from devai.sandbox.models import SandboxStatus
from devai.sandbox.portable import build_portable_runtime_manifests, portable_runtime_endpoint
from devai.sandbox.snapshot import capture
from devai.sandbox.workspace import build_workspace_manifests, workspace_service_host
from devai.sandbox.workspace_client import WorkspaceClient

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)


class _StatusStore(Protocol):
    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None: ...


class SandboxProvisioner:
    def __init__(self, runtime: Any, store: _StatusStore, *, broker: Any = None) -> None:
        self._runtime = runtime
        self._store = store
        self._broker = broker

    async def provision(self, record: SandboxRecord) -> SandboxRecord:
        await self._set(record, SandboxStatus.PROVISIONING)
        namespace = getattr(getattr(self._runtime, "config", None), "namespace", "devai")
        try:
            await self._runtime.connect()
            for manifest in build_isolation_manifests(record, namespace=namespace):
                await self._runtime.apply_manifest(manifest)
            await self._provision_identity(record, namespace)
            detail = await self._provision_egress(record, namespace)
            detail.update(await self._provision_agent_runtime(record, namespace) or {})
            detail.update(await self._provision_workspace(record, namespace) or {})
        except Exception as e:  # noqa: BLE001 — a cluster failure is a sandbox outcome, not a crash
            logger.warning("sandbox %s: provisioning failed", record.id, exc_info=True)
            return await self._set(record, SandboxStatus.FAILED, {"error": str(e)})
        return await self._set(record, SandboxStatus.READY, detail)

    async def _provision_identity(self, record: SandboxRecord, namespace: str) -> None:
        """The sandbox's own Secret: how it proves to the broker that it is itself.

        This is the same Secret every rescoped ``secretKeyRef`` points at, so a
        sandbox that is given a credential and one that is given none differ
        only in the Secret's contents.
        """
        await self._runtime.apply_manifest(build_sandbox_secret(record, namespace=namespace))

    async def _provision_egress(self, record: SandboxRecord, namespace: str) -> dict[str, Any]:
        """Every sandbox gets one — the NetworkPolicy leaves no other way out."""
        for manifest in build_proxy_manifests(record, namespace=namespace):
            await self._runtime.apply_manifest(manifest)
        return {
            "egress": {
                "proxy": proxy_service_host(record.id, namespace=namespace),
                "added_domains": list(record.spec.allow_domains),
            }
        }

    async def _provision_workspace(self, record: SandboxRecord, namespace: str) -> dict[str, Any] | None:
        if not record.spec.workspace:
            return None
        # The token lives in the Secret and nowhere else — not in the sandbox row,
        # which is read by the dashboard and the audit log.
        token = secrets.token_urlsafe(32)
        git_token = await self._clone_token(record)
        for manifest in build_workspace_manifests(record, namespace=namespace, token=token):
            if git_token and manifest["kind"] == "Secret":
                manifest["stringData"]["git_token"] = git_token
            await self._runtime.apply_manifest(manifest)
        return {"workspace": {"endpoint": workspace_service_host(record.id, namespace=namespace)}}

    async def _provision_agent_runtime(self, record: SandboxRecord, namespace: str) -> dict[str, Any] | None:
        snapshot = record.spec.import_snapshot
        if snapshot is None:
            return None
        for manifest in build_portable_runtime_manifests(record, namespace=namespace):
            await self._runtime.apply_manifest(manifest)
        return {
            "agent_runtime": {
                "endpoint": portable_runtime_endpoint(record, namespace=namespace),
                "protocol": snapshot.runtime.get("protocol"),
                "type": snapshot.runtime.get("type"),
                "agent_digest": snapshot.agent_digest,
            }
        }

    async def _clone_token(self, record: SandboxRecord) -> str:
        """The seed's credential, minted through the broker like any other.

        A repo the sandbox may not reach raises, which fails provisioning —
        starting from an empty tree would silently measure something else.
        """
        repo = record.spec.repo
        if repo is None:
            return ""
        if self._broker is None:
            raise RuntimeError(f"sandbox {record.id} pins a repo but no credential broker is configured")
        _, secret = await self._broker.grant(record, kind="scm", scope=repo.scope)
        return str(secret)

    async def teardown(self, record: SandboxRecord) -> SandboxRecord:
        await self._set(record, SandboxStatus.DESTROYING)
        namespace = getattr(getattr(self._runtime, "config", None), "namespace", "devai")
        if self._broker is not None:
            await self._broker.revoke_all(record.id)
        manifests = build_isolation_manifests(record, namespace=namespace)
        manifests += [build_sandbox_secret(record, namespace=namespace, token="-")]
        manifests += build_proxy_manifests(record, namespace=namespace)
        if record.spec.import_snapshot is not None:
            manifests += build_portable_runtime_manifests(record, namespace=namespace)
        detail = await self._snapshot(record, namespace)
        if record.spec.workspace:
            # Deleting needs the kinds and names only, so no token is minted here.
            manifests += build_workspace_manifests(record, namespace=namespace, token="-")
        for manifest in manifests:
            try:
                await self._runtime.delete_manifest(manifest["kind"], manifest["metadata"]["name"], namespace)
            except Exception:  # noqa: BLE001 — already gone is the desired end state
                logger.debug("sandbox %s: %s already absent", record.id, manifest["kind"])
        return await self._set(record, SandboxStatus.DESTROYED, detail)

    async def _snapshot(self, record: SandboxRecord, namespace: str) -> dict[str, Any] | None:
        """What the workspace leaves behind, taken before the objects go.

        A failed capture is recorded, never raised: a sandbox that cannot be
        read is exactly the one that most needs deleting.
        """
        if not record.spec.workspace:
            return None
        try:
            token = await self._runtime.read_secret_key(f"devai-sandbox-ws-{record.id}", "token")
            client = WorkspaceClient(workspace_service_host(record.id, namespace=namespace), token=token)
            return {"snapshot": await capture(client)}
        except Exception as e:  # noqa: BLE001
            logger.warning("sandbox %s: snapshot failed", record.id, exc_info=True)
            return {"snapshot": {"errors": {"workspace": str(e)}}}

    async def _set(
        self, record: SandboxRecord, status: SandboxStatus, detail: dict[str, Any] | None = None
    ) -> SandboxRecord:
        await self._store.set_sandbox_status(record.id, status.value, detail)
        return record.model_copy(update={"status": status, "detail": detail or record.detail})


__all__ = ["SandboxProvisioner"]
