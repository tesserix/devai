"""GitOpsAdapter ABC + the shared kubectl runner.

GitOps controllers (Argo CD, Kargo, Flux CD) are external systems, so they
get a proper adapter family: one ABC, one file per backend, a Noop, and a
factory that never raises. Swap the deploy plane with one env var
(`DEVAI_GITOPS_PROVIDER`); expose several at once through the gitops MCP
domain (`DEVAI_GITOPS_MCP_PROVIDERS`).

All three first-party backends talk to the Kubernetes API via kubectl —
the same approach `devai.services.argocd` proved out: no controller API
server credentials, no extra CLIs, works wherever the pod's service
account has the CRD RBAC. The DevAI ClusterRole needs (see
tesserix-k8s/charts/apps/devai-api/templates/clusterrole.yaml):

  - argoproj.io applications            get/list/watch/patch
  - kargo.akuity.io projects/stages/freights/promotions/warehouses
                                        get/list/watch/create/patch (+promote)
  - {kustomize,helm}.toolkit.fluxcd.io  get/list/watch/patch
  - source.toolkit.fluxcd.io            get/list/watch/patch

Mutating operations (sync, promote, rollback, reconcile, suspend) are
gated by `settings.gitops_mutations_enabled` — when off, every backend
refuses with a clear message instead of acting. Methods never raise out
of the family: failures come back as `{"ok": False, "error": ...}` so a
tool call degrades into an answer the agent can reason about.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

KUBECTL_TIMEOUT = 30


async def kubectl(*args: str, timeout: int = KUBECTL_TIMEOUT, stdin: str = "") -> str:
    """Run kubectl, return stdout. Raises RuntimeError on a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "kubectl",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(stdin.encode() if stdin else None), timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl failed (rc={proc.returncode}): {stderr.decode().strip()[:500]}")
    return stdout.decode().strip()


async def kubectl_json(*args: str, timeout: int = KUBECTL_TIMEOUT) -> dict[str, Any]:
    """Run kubectl with -o json and parse the result."""
    out = await kubectl(*args, "-o", "json", timeout=timeout)
    return json.loads(out) if out else {}


def err(detail: str) -> dict[str, Any]:
    """The family's uniform failure shape — degrade, never raise."""
    return {"ok": False, "error": detail}


class GitOpsAdapter(ABC):
    """The minimum surface every GitOps backend implements.

    `scope` narrows the query to the backend's natural grouping: an Argo CD
    *project*, a Kargo *project* (its namespace), a Flux *namespace*. Blank
    means the backend's default/all.
    """

    provider: str = "gitops"

    def __init__(self, *, mutations_enabled: bool = True) -> None:
        self._mutations_enabled = mutations_enabled

    # -- family-wide helpers -------------------------------------------------

    def _mutation_blocked(self) -> dict[str, Any] | None:
        if self._mutations_enabled:
            return None
        return err(
            f"{self.provider}: mutating GitOps operations are disabled "
            "(DEVAI_GITOPS_MUTATIONS_ENABLED=false) — report the intended action instead"
        )

    async def close(self) -> None:  # kubectl is per-call; nothing to release
        return

    async def health_check(self) -> dict[str, Any]:
        """`{"ok", "provider", "detail"}` — never raises."""
        try:
            targets = await self.list_targets()
            n = len(targets) if isinstance(targets, list) else 0
            return {"ok": True, "provider": self.provider, "detail": f"{n} target(s) visible"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider, "detail": str(e)[:300]}

    # -- the common surface ---------------------------------------------------

    @abstractmethod
    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        """Deployable units: Argo CD apps / Kargo stages / Flux kustomizations+helmreleases."""

    @abstractmethod
    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        """Detailed sync/health/revision status for one target."""

    @abstractmethod
    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        """Converge the target now: argocd sync / kargo promote latest / flux reconcile."""

    @abstractmethod
    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        """Past deployments/promotions for the target, newest last."""

    @abstractmethod
    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        """Return to a previous revision. Backends that can't (Flux) say so."""


__all__ = ["KUBECTL_TIMEOUT", "GitOpsAdapter", "err", "kubectl", "kubectl_json"]
