"""Argo CD backend — Application CRDs via the Kubernetes API.

Wraps the proven `devai.services.argocd.ArgocdClient` (kubectl against
`applications.argoproj.io`) behind the family ABC, adding the mutation
gate and the uniform degrade-to-dict error shape.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.adapters.gitops.base import GitOpsAdapter, err

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class ArgoCDGitOpsAdapter(GitOpsAdapter):
    provider = "argocd"

    def __init__(self, settings: Settings, *, mutations_enabled: bool = True) -> None:
        super().__init__(mutations_enabled=mutations_enabled)
        from devai.services.argocd import ArgocdClient  # internal, not a vendor SDK

        self._client = ArgocdClient(settings)
        self.namespace = settings.argocd_namespace

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        return await self._client.list_apps(project=scope or None)

    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        try:
            status = await self._client.get_app_status(name)
        except Exception as e:  # noqa: BLE001
            return err(f"argocd: cannot read application {name!r}: {e}")
        # Recent conditions help an agent diagnose a stuck sync without a second call.
        try:
            app = await self._client.get_app(name)
            conditions = app.get("status", {}).get("conditions", [])
            if conditions:
                status["conditions"] = [
                    {"type": c.get("type", ""), "message": (c.get("message") or "")[:300]} for c in conditions[:5]
                ]
        except Exception:  # noqa: BLE001 — conditions are best-effort garnish
            pass
        return status

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        if blocked := self._mutation_blocked():
            return blocked
        try:
            result = await self._client.trigger_sync(name)
            logger.info("gitops/argocd: sync triggered for %s", name)
            return {"ok": True, **result}
        except Exception as e:  # noqa: BLE001
            return err(f"argocd: sync failed for {name!r}: {e}")

    async def wait_for_sync(self, name: str, timeout: int | None = None) -> dict[str, Any]:
        """Poll until Synced+Healthy / Degraded / Failed / timeout."""
        try:
            return await self._client.wait_for_sync(name, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return err(f"argocd: wait_for_sync failed for {name!r}: {e}")

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        try:
            return await self._client.get_history(name)
        except Exception as e:  # noqa: BLE001
            return [err(f"argocd: cannot read history for {name!r}: {e}")]

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        if blocked := self._mutation_blocked():
            return blocked
        try:
            rev_id = int(revision) if revision else None
            result = await self._client.rollback(name, revision_id=rev_id)
            if "error" in result:
                return err(f"argocd: {result['error']}")
            logger.warning("gitops/argocd: ROLLBACK initiated for %s → %s", name, result.get("target_revision"))
            return {"ok": True, **result}
        except ValueError:
            return err(f"argocd: rollback revision must be a history id (got {revision!r}) — call history first")
        except Exception as e:  # noqa: BLE001
            return err(f"argocd: rollback failed for {name!r}: {e}")


__all__ = ["ArgoCDGitOpsAdapter"]
