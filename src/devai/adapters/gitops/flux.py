"""Flux CD backend — Kustomizations / HelmReleases / Sources via CRDs.

Flux has no API server of its own: everything is CRD state plus an
annotation-based reconcile trigger (`reconcile.fluxcd.io/requestedAt`),
which maps cleanly onto kubectl.

Mapping onto the family surface:
  - list_targets(scope=ns) → Kustomizations + HelmReleases
  - get_target(name)       → Ready condition, last applied revision, suspension
  - sync(name)             → annotate the reconcile request
  - history(name)          → limited: Flux keeps only lastApplied/lastAttempted
  - rollback               → unsupported by design (revert the Git commit instead) —
                             the error says exactly that so agents do the right thing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from devai.adapters.gitops.base import GitOpsAdapter, err, kubectl, kubectl_json

logger = logging.getLogger(__name__)

# kind keyword (what an agent says) → fully-qualified CRD resource
_KINDS = {
    "kustomization": "kustomizations.kustomize.toolkit.fluxcd.io",
    "helmrelease": "helmreleases.helm.toolkit.fluxcd.io",
    "gitrepository": "gitrepositories.source.toolkit.fluxcd.io",
    "helmrepository": "helmrepositories.source.toolkit.fluxcd.io",
    "ocirepository": "ocirepositories.source.toolkit.fluxcd.io",
}
_SOURCE_KINDS = ("gitrepository", "helmrepository", "ocirepository")


def _ready(obj: dict[str, Any]) -> dict[str, str]:
    for c in obj.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready":
            return {"ready": c.get("status", "Unknown"), "message": (c.get("message") or "")[:300]}
    return {"ready": "Unknown", "message": ""}


def _summary(obj: dict[str, Any], kind: str) -> dict[str, Any]:
    meta, status, spec = obj.get("metadata", {}), obj.get("status", {}), obj.get("spec", {})
    return {
        "kind": kind,
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "suspended": bool(spec.get("suspend", False)),
        "revision": status.get("lastAppliedRevision", "") or status.get("lastAttemptedRevision", ""),
        **_ready(obj),
    }


class FluxGitOpsAdapter(GitOpsAdapter):
    provider = "flux"

    def __init__(
        self,
        *,
        default_namespace: str = "flux-system",
        mutations_enabled: bool = True,
        cluster: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(mutations_enabled=mutations_enabled)
        self.default_namespace = default_namespace
        self._cluster = cluster  # user-connected cluster (None = in-cluster)

    @staticmethod
    def _resource(kind: str) -> str:
        return _KINDS.get(kind.strip().lower().rstrip("s"), "")

    async def _list_kind(self, kind: str, namespace: str) -> list[dict[str, Any]]:
        resource = self._resource(kind)
        if not resource:
            return [err(f"flux: unknown kind {kind!r} (use {', '.join(_KINDS)})")]
        args = ["get", resource]
        args += ["-n", namespace] if namespace else ["-A"]
        try:
            result = await kubectl_json(*args, cluster=self._cluster)
        except Exception as e:  # noqa: BLE001
            return [err(f"flux: cannot list {kind}: {e}")]
        return [_summary(o, kind) for o in result.get("items", [])]

    async def list_kustomizations(self, namespace: str = "") -> list[dict[str, Any]]:
        return await self._list_kind("kustomization", namespace)

    async def list_helmreleases(self, namespace: str = "") -> list[dict[str, Any]]:
        return await self._list_kind("helmrelease", namespace)

    async def list_sources(self, namespace: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind in _SOURCE_KINDS:
            rows = await self._list_kind(kind, namespace)
            out.extend(r for r in rows if r.get("ok", True) is not False)  # drop per-kind errors (CRD may be absent)
        return out

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        out = [r for r in await self.list_kustomizations(scope) if r.get("ok", True) is not False]
        out += [r for r in await self.list_helmreleases(scope) if r.get("ok", True) is not False]
        return out

    async def get_target(self, name: str, scope: str = "", kind: str = "") -> dict[str, Any]:
        namespace = scope or self.default_namespace
        kinds = [kind] if kind else ["kustomization", "helmrelease"]
        for k in kinds:
            resource = self._resource(k)
            if not resource:
                return err(f"flux: unknown kind {k!r}")
            try:
                obj = await kubectl_json("get", resource, name, "-n", namespace, cluster=self._cluster)
            except Exception:  # noqa: BLE001 — try the next kind
                continue
            detail = _summary(obj, k)
            detail["conditions"] = [
                {"type": c.get("type", ""), "status": c.get("status", ""), "message": (c.get("message") or "")[:200]}
                for c in obj.get("status", {}).get("conditions", [])[:6]
            ]
            detail["source"] = obj.get("spec", {}).get("sourceRef", {}) or obj.get("spec", {}).get("chart", {})
            return detail
        return err(f"flux: no kustomization/helmrelease named {name!r} in namespace {namespace!r}")

    async def reconcile(self, name: str, namespace: str = "", kind: str = "kustomization") -> dict[str, Any]:
        """Request an immediate reconcile (what `flux reconcile` does)."""
        if blocked := self._mutation_blocked():
            return blocked
        resource = self._resource(kind)
        if not resource:
            return err(f"flux: unknown kind {kind!r} (use {', '.join(_KINDS)})")
        ns = namespace or self.default_namespace
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            await kubectl(
                "annotate",
                resource,
                name,
                "-n",
                ns,
                f"reconcile.fluxcd.io/requestedAt={stamp}",
                "--overwrite",
                cluster=self._cluster,
            )
            logger.info("gitops/flux: reconcile requested for %s/%s (%s)", ns, name, kind)
            return {"ok": True, "kind": kind, "name": name, "namespace": ns, "requested_at": stamp}
        except Exception as e:  # noqa: BLE001
            return err(f"flux: reconcile failed for {kind}/{name}: {e}")

    async def set_suspended(
        self, name: str, suspended: bool, namespace: str = "", kind: str = "kustomization"
    ) -> dict[str, Any]:
        """Suspend (freeze) or resume reconciliation of a Flux object."""
        if blocked := self._mutation_blocked():
            return blocked
        resource = self._resource(kind)
        if not resource:
            return err(f"flux: unknown kind {kind!r} (use {', '.join(_KINDS)})")
        ns = namespace or self.default_namespace
        patch = '{"spec":{"suspend":' + ("true" if suspended else "false") + "}}"
        try:
            await kubectl("patch", resource, name, "-n", ns, "--type", "merge", "-p", patch, cluster=self._cluster)
            verb = "suspended" if suspended else "resumed"
            logger.warning("gitops/flux: %s %s/%s (%s)", verb.upper(), ns, name, kind)
            return {"ok": True, "kind": kind, "name": name, "namespace": ns, "suspended": suspended}
        except Exception as e:  # noqa: BLE001
            return err(f"flux: suspend/resume failed for {kind}/{name}: {e}")

    # -- common surface -----------------------------------------------------------

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        result = await self.reconcile(name, namespace=scope, kind="kustomization")
        if result.get("ok"):
            return result
        return await self.reconcile(name, namespace=scope, kind="helmrelease")

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        detail = await self.get_target(name, scope)
        if detail.get("ok", True) is False:
            return [detail]
        return [{"revision": detail.get("revision", ""), "ready": detail.get("ready", ""), "note": "Flux keeps only the last applied/attempted revision; full history lives in Git."}]

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        return err(
            "flux: rollback is Git-driven by design — revert the offending commit in the source "
            "repository (git revert) and Flux will reconcile back. As a stopgap you can suspend "
            "the object (flux_suspend) to stop further drift."
        )


__all__ = ["FluxGitOpsAdapter"]
