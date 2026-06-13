"""ArgoCD client using kubectl to manage Application CRDs.

Uses kubectl (available in-cluster via the service account) to:
- Query ArgoCD Application sync/health status
- Trigger application sync
- Wait for sync completion and healthy status
- Rollback to a previous revision

This avoids needing the ArgoCD API server credentials or the argocd CLI —
it talks directly to the Kubernetes API to read/patch Application CRDs,
which is the most reliable approach when running in the same cluster.

The DevAI service account needs a ClusterRole with:
  - apiGroups: ["argoproj.io"]
    resources: ["applications"]
    verbs: ["get", "list", "watch", "patch"]
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

KUBECTL_TIMEOUT = 30


class ArgocdClient:
    """Lightweight ArgoCD client using kubectl for Application CRD management.

    All public methods are traced via LangSmith when enabled.
    """

    def __init__(
        self,
        config: Settings,
        *,
        kubectl_extra: list[str] | None = None,
        namespace: str = "",
    ) -> None:
        self.namespace = namespace or config.argocd_namespace
        self.sync_timeout = config.argocd_sync_timeout
        self.health_timeout = config.argocd_health_timeout
        # Extra kubectl flags (e.g. --server/--token targeting a
        # user-connected cluster from Settings → Kubernetes Clusters).
        self._kubectl_extra = list(kubectl_extra or [])

        # Apply tracing decorators to public methods
        self._apply_tracing()

    def _apply_tracing(self) -> None:
        """Wrap public methods with LangSmith tracing if enabled."""
        from devai.services.tracing import is_tracing_enabled

        if not is_tracing_enabled():
            return

        try:
            from langsmith import traceable

            for method_name in (
                "get_app_status",
                "list_apps",
                "trigger_sync",
                "wait_for_sync",
                "wait_for_health",
                "rollback",
            ):
                original = getattr(self, method_name)
                traced = traceable(
                    name=f"argocd.{method_name}",
                    run_type="tool",
                )(original)
                setattr(self, method_name, traced)
        except ImportError:
            pass

    async def _kubectl(self, *args: str) -> str:
        """Execute a kubectl command and return stdout."""
        cmd = ["kubectl", *self._kubectl_extra, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=KUBECTL_TIMEOUT)

        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(f"kubectl failed (rc={proc.returncode}): {err}")

        return stdout.decode().strip()

    async def _kubectl_json(self, *args: str) -> dict[str, Any]:
        """Execute kubectl and parse JSON output."""
        output = await self._kubectl(*args, "-o", "json")
        return json.loads(output)

    # --- Application Status ---

    async def get_app(self, app_name: str) -> dict[str, Any]:
        """Get the full ArgoCD Application resource."""
        return await self._kubectl_json(
            "get",
            "application",
            app_name,
            "-n",
            self.namespace,
        )

    async def get_app_status(self, app_name: str) -> dict[str, Any]:
        """Get a summary of an ArgoCD Application's current status."""
        app = await self.get_app(app_name)
        status = app.get("status", {})
        operation = status.get("operationState", {})

        return {
            "name": app_name,
            "sync_status": status.get("sync", {}).get("status", "Unknown"),
            "health_status": status.get("health", {}).get("status", "Unknown"),
            "revision": status.get("sync", {}).get("revision", ""),
            "operation_phase": operation.get("phase", ""),
            "operation_message": operation.get("message", ""),
            "started_at": operation.get("startedAt", ""),
            "finished_at": operation.get("finishedAt", ""),
            "source_repo": app.get("spec", {}).get("source", {}).get("repoURL", ""),
            "source_path": app.get("spec", {}).get("source", {}).get("path", ""),
            "destination_namespace": app.get("spec", {}).get("destination", {}).get("namespace", ""),
        }

    async def list_apps(self, project: str | None = None) -> list[dict[str, Any]]:
        """List all ArgoCD Applications, optionally filtered by project."""
        result = await self._kubectl_json(
            "get",
            "applications",
            "-n",
            self.namespace,
        )
        apps = result.get("items", [])

        summaries = []
        for app in apps:
            name = app.get("metadata", {}).get("name", "")
            status = app.get("status", {})
            summaries.append(
                {
                    "name": name,
                    "project": app.get("spec", {}).get("project", "default"),
                    "sync_status": status.get("sync", {}).get("status", "Unknown"),
                    "health_status": status.get("health", {}).get("status", "Unknown"),
                    "namespace": app.get("spec", {}).get("destination", {}).get("namespace", ""),
                }
            )

        if project:
            summaries = [s for s in summaries if s["project"] == project]

        return summaries

    # --- Sync Operations ---

    async def trigger_sync(self, app_name: str) -> dict[str, Any]:
        """Trigger an ArgoCD Application sync via kubectl patch."""
        # Patch the Application to trigger a sync by setting the operation field
        patch = json.dumps(
            {
                "operation": {
                    "initiatedBy": {"username": "devai-agent"},
                    "sync": {
                        "syncStrategy": {
                            "apply": {"force": False},
                        },
                    },
                },
            }
        )

        await self._kubectl(
            "patch",
            "application",
            app_name,
            "-n",
            self.namespace,
            "--type",
            "merge",
            "-p",
            patch,
        )

        logger.info("Triggered sync for ArgoCD app: %s", app_name)
        return {"app": app_name, "action": "sync_triggered"}

    async def wait_for_sync(self, app_name: str, timeout: int | None = None) -> dict[str, Any]:
        """Poll until the ArgoCD Application is synced and healthy.

        Returns the final status dict with sync/health results.
        """
        timeout = timeout or self.sync_timeout
        elapsed = 0
        poll_interval = 10

        while elapsed < timeout:
            try:
                status = await self.get_app_status(app_name)

                sync = status["sync_status"]
                health = status["health_status"]
                phase = status["operation_phase"]

                logger.debug(
                    "ArgoCD %s: sync=%s health=%s phase=%s (elapsed=%ds)",
                    app_name,
                    sync,
                    health,
                    phase,
                    elapsed,
                )

                # Sync complete + healthy = done
                if sync == "Synced" and health == "Healthy":
                    logger.info(
                        "ArgoCD app %s is synced and healthy (took %ds)",
                        app_name,
                        elapsed,
                    )
                    return {**status, "result": "healthy", "elapsed": elapsed}

                # Sync complete but degraded/missing
                if sync == "Synced" and health in ("Degraded", "Missing"):
                    logger.warning("ArgoCD app %s synced but health=%s", app_name, health)
                    return {**status, "result": "degraded", "elapsed": elapsed}

                # Operation failed
                if phase in ("Failed", "Error"):
                    logger.error("ArgoCD sync failed for %s: %s", app_name, status["operation_message"])
                    return {**status, "result": "failed", "elapsed": elapsed}

            except Exception as e:
                logger.warning("Failed to check ArgoCD status for %s: %s", app_name, e)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout
        logger.warning("ArgoCD sync timeout for %s after %ds", app_name, timeout)
        try:
            final = await self.get_app_status(app_name)
            return {**final, "result": "timeout", "elapsed": elapsed}
        except Exception:
            return {"name": app_name, "result": "timeout", "elapsed": elapsed}

    async def wait_for_health(self, app_name: str, timeout: int | None = None) -> dict[str, Any]:
        """Wait specifically for healthy status (after sync is already done)."""
        timeout = timeout or self.health_timeout
        elapsed = 0
        poll_interval = 5

        while elapsed < timeout:
            try:
                status = await self.get_app_status(app_name)
                health = status["health_status"]

                if health == "Healthy":
                    return {**status, "result": "healthy", "elapsed": elapsed}
                if health == "Degraded":
                    return {**status, "result": "degraded", "elapsed": elapsed}

            except Exception as e:
                logger.warning("Health check failed for %s: %s", app_name, e)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return {"name": app_name, "result": "timeout", "elapsed": elapsed}

    # --- Rollback ---

    async def get_history(self, app_name: str) -> list[dict[str, Any]]:
        """Get the deployment history of an ArgoCD Application."""
        app = await self.get_app(app_name)
        history = app.get("status", {}).get("history", [])
        return [
            {
                "id": entry.get("id"),
                "revision": entry.get("revision", "")[:12],
                "deployed_at": entry.get("deployedAt", ""),
                "source": entry.get("source", {}).get("repoURL", ""),
            }
            for entry in history
        ]

    async def rollback(self, app_name: str, revision_id: int | None = None) -> dict[str, Any]:
        """Rollback to a previous revision.

        If revision_id is None, rolls back to the previous deployment.
        """
        history = await self.get_history(app_name)
        if not history:
            return {"error": "No deployment history found"}

        if revision_id is None:
            # Roll back to the second-to-last entry
            if len(history) < 2:
                return {"error": "No previous revision to rollback to"}
            target = history[-2]
        else:
            target = next((h for h in history if h["id"] == revision_id), None)
            if not target:
                return {"error": f"Revision {revision_id} not found in history"}

        target_revision = target["revision"]

        # Patch the Application source to use the target revision
        patch = json.dumps(
            {
                "spec": {
                    "source": {
                        "targetRevision": target_revision,
                    },
                },
            }
        )

        await self._kubectl(
            "patch",
            "application",
            app_name,
            "-n",
            self.namespace,
            "--type",
            "merge",
            "-p",
            patch,
        )

        # Trigger sync to the target revision
        await self.trigger_sync(app_name)

        logger.info("Rolling back %s to revision %s", app_name, target_revision)
        return {
            "app": app_name,
            "action": "rollback",
            "target_revision": target_revision,
            "deployed_at": target["deployed_at"],
        }
