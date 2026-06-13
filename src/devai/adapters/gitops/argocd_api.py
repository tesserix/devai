"""Argo CD via its REST API — for managed/external instances (no kube access).

When a user connects an Argo CD by server URL + token (Settings → Argo CD,
mode ``api``) rather than a cluster, this talks straight to the Argo CD API
server (``/api/v1/applications``) over HTTPS with a bearer token. Same
GitOpsAdapter surface as the kubectl-backed backend, so the tools are
identical; only the transport differs.

httpx is imported lazily (adapter-family rule). Every call degrades to the
family's ``{"ok": False, "error": ...}`` shape — never an exception into the
agent loop.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.gitops.base import GitOpsAdapter, err

logger = logging.getLogger(__name__)


class ArgoCDApiAdapter(GitOpsAdapter):
    provider = "argocd"

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        mutations_enabled: bool = True,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(mutations_enabled=mutations_enabled)
        self._base = server_url.rstrip("/")
        self._token = token
        self._verify = verify_tls
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        import httpx  # lazy

        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout) as client:
            resp = await client.request(method, f"{self._base}{path}", headers=headers, **kw)
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    @staticmethod
    def _summary(app: dict[str, Any]) -> dict[str, Any]:
        meta, status, spec = app.get("metadata", {}), app.get("status", {}), app.get("spec", {})
        op = status.get("operationState", {})
        return {
            "name": meta.get("name", ""),
            "project": spec.get("project", "default"),
            "sync_status": status.get("sync", {}).get("status", "Unknown"),
            "health_status": status.get("health", {}).get("status", "Unknown"),
            "revision": status.get("sync", {}).get("revision", ""),
            "operation_phase": op.get("phase", ""),
            "operation_message": (op.get("message") or "")[:300],
            "source_repo": spec.get("source", {}).get("repoURL", ""),
            "namespace": spec.get("destination", {}).get("namespace", ""),
        }

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        try:
            data = await self._request("GET", "/api/v1/applications")
        except Exception as e:  # noqa: BLE001
            return [err(f"argocd-api: list failed: {e}")]
        apps = [self._summary(a) for a in data.get("items", []) or []]
        return [a for a in apps if not scope or a["project"] == scope]

    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        try:
            app = await self._request("GET", f"/api/v1/applications/{name}")
        except Exception as e:  # noqa: BLE001
            return err(f"argocd-api: cannot read application {name!r}: {e}")
        out = self._summary(app)
        conds = app.get("status", {}).get("conditions", []) or []
        if conds:
            out["conditions"] = [{"type": c.get("type", ""), "message": (c.get("message") or "")[:300]} for c in conds[:5]]
        return out

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        if blocked := self._mutation_blocked():
            return blocked
        try:
            await self._request("POST", f"/api/v1/applications/{name}/sync", json={})
            logger.info("gitops/argocd-api: sync triggered for %s", name)
            return {"ok": True, "app": name, "action": "sync_triggered"}
        except Exception as e:  # noqa: BLE001
            return err(f"argocd-api: sync failed for {name!r}: {e}")

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        try:
            app = await self._request("GET", f"/api/v1/applications/{name}")
        except Exception as e:  # noqa: BLE001
            return [err(f"argocd-api: cannot read history for {name!r}: {e}")]
        hist = app.get("status", {}).get("history", []) or []
        return [
            {"id": h.get("id"), "revision": (h.get("revision") or "")[:12], "deployed_at": h.get("deployedAt", "")}
            for h in hist
        ]

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        if blocked := self._mutation_blocked():
            return blocked
        hist = await self.history(name)
        if hist and hist[0].get("ok", True) is False:
            return hist[0]
        if not hist:
            return err(f"argocd-api: no deployment history for {name!r}")
        if revision:
            try:
                target_id = int(revision)
            except ValueError:
                return err(f"argocd-api: rollback id must be a history id (got {revision!r})")
        else:
            if len(hist) < 2:
                return err(f"argocd-api: no previous revision to roll {name!r} back to")
            target_id = hist[-2].get("id")
        try:
            await self._request("POST", f"/api/v1/applications/{name}/rollback", json={"id": target_id})
            logger.warning("gitops/argocd-api: ROLLBACK %s → history id %s", name, target_id)
            return {"ok": True, "app": name, "action": "rollback", "target_id": target_id}
        except Exception as e:  # noqa: BLE001
            return err(f"argocd-api: rollback failed for {name!r}: {e}")

    async def wait_for_sync(self, name: str, timeout: int | None = None) -> dict[str, Any]:
        """Single status read (the API caller polls); mirrors the kubectl backend's shape."""
        status = await self.get_target(name, "")
        if status.get("ok", True) is False:
            return status
        synced = status.get("sync_status") == "Synced"
        healthy = status.get("health_status") == "Healthy"
        result = "healthy" if (synced and healthy) else ("degraded" if synced else "in_progress")
        return {**status, "result": result}


__all__ = ["ArgoCDApiAdapter"]
