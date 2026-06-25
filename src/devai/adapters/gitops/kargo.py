"""Kargo backend — promotion-based delivery via kargo.akuity.io CRDs.

Kargo models delivery as *freight* (an immutable set of artifacts — image
tags, chart versions, git commits) promoted through *stages* (dev → staging
→ prod) inside a *project* (a namespace). Promoting = creating a Promotion
CR; the Kargo controller does the rest.

Mapping onto the family surface:
  - list_targets(scope=project) → stages
  - get_target(stage)           → stage phase/health + current freight
  - sync(stage)                 → promote the NEWEST freight into the stage
  - history(stage)              → past promotions
  - rollback(stage, revision)   → promote an older freight (revision = freight name/alias)

RBAC: Kargo additionally authorizes promotion via the `promote` verb on
Stages — the DevAI ClusterRole carries it.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.gitops.base import GitOpsAdapter, err, kubectl, kubectl_json

logger = logging.getLogger(__name__)

_GROUP = "kargo.akuity.io"


def _freight_summary(f: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Freight CR into what an agent needs to pick one."""
    images = [f"{i.get('repoURL', '')}:{i.get('tag', '')}" for i in f.get("images", [])]
    commits = [f"{c.get('repoURL', '')}@{(c.get('id') or '')[:10]}" for c in f.get("commits", [])]
    charts = [f"{c.get('repoURL', '')}/{c.get('name', '')}:{c.get('version', '')}" for c in f.get("charts", [])]
    return {
        "name": f.get("metadata", {}).get("name", ""),
        "alias": f.get("alias", "") or f.get("metadata", {}).get("labels", {}).get("kargo.akuity.io/alias", ""),
        "warehouse": f.get("origin", {}).get("name", ""),
        "created": f.get("metadata", {}).get("creationTimestamp", ""),
        "images": images,
        "commits": commits,
        "charts": charts,
    }


class KargoGitOpsAdapter(GitOpsAdapter):
    provider = "kargo"

    def __init__(
        self,
        *,
        default_project: str = "",
        mutations_enabled: bool = True,
        cluster: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(mutations_enabled=mutations_enabled)
        self.default_project = default_project
        self._cluster = cluster  # user-connected cluster (None = in-cluster)

    def _project(self, scope: str) -> str:
        return scope or self.default_project

    # -- discovery -------------------------------------------------------------

    async def list_projects(self) -> list[dict[str, Any]]:
        try:
            result = await kubectl_json("get", f"projects.{_GROUP}", cluster=self._cluster)
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo: cannot list projects: {e}")]
        return [
            {
                "name": p.get("metadata", {}).get("name", ""),
                "phase": p.get("status", {}).get("phase", ""),
            }
            for p in result.get("items", [])
        ]

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        project = self._project(scope)
        args = ["get", f"stages.{_GROUP}"]
        args += ["-n", project] if project else ["-A"]
        try:
            result = await kubectl_json(*args, cluster=self._cluster)
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo: cannot list stages: {e}")]
        out = []
        for s in result.get("items", []):
            status = s.get("status", {})
            current = (status.get("freightHistory") or [{}])[0]
            freight_names = [fr.get("name", "") for fr in (current.get("items") or {}).values()] if current else []
            out.append(
                {
                    "stage": s.get("metadata", {}).get("name", ""),
                    "project": s.get("metadata", {}).get("namespace", ""),
                    "phase": status.get("phase", ""),
                    "health": (status.get("health") or {}).get("status", "Unknown"),
                    "current_freight": freight_names,
                    "last_promotion": (status.get("lastPromotion") or {}).get("name", ""),
                }
            )
        return out

    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        project = self._project(scope)
        if not project:
            return err("kargo: a project (namespace) is required — pass scope/project")
        try:
            s = await kubectl_json("get", f"stages.{_GROUP}", name, "-n", project, cluster=self._cluster)
        except Exception as e:  # noqa: BLE001
            return err(f"kargo: cannot read stage {name!r} in project {project!r}: {e}")
        status = s.get("status", {})
        last = status.get("lastPromotion") or {}
        return {
            "stage": name,
            "project": project,
            "phase": status.get("phase", ""),
            "health": (status.get("health") or {}).get("status", "Unknown"),
            "message": (status.get("message") or "")[:300],
            "requested_freight": [
                {
                    "origin": (r.get("origin") or {}).get("name", ""),
                    "direct": (r.get("sources") or {}).get("direct", False),
                    "upstream_stages": (r.get("sources") or {}).get("stages", []),
                }
                for r in s.get("spec", {}).get("requestedFreight", [])
            ],
            "last_promotion": {
                "name": last.get("name", ""),
                "phase": (last.get("status") or {}).get("phase", ""),
                "freight": (last.get("freight") or {}).get("name", ""),
                "finished_at": (last.get("status") or {}).get("finishedAt", ""),
            },
        }

    async def list_freight(self, project: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not project:
            return [err("kargo: a project (namespace) is required to list freight")]
        try:
            result = await kubectl_json(
                "get",
                f"freights.{_GROUP}",
                "-n",
                project,
                "--sort-by=.metadata.creationTimestamp",
                cluster=self._cluster,
            )
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo: cannot list freight in {project!r}: {e}")]
        items = result.get("items", [])
        return [_freight_summary(f) for f in items[-limit:]]

    # -- promotion --------------------------------------------------------------

    async def promote(self, project: str, stage: str, freight: str) -> dict[str, Any]:
        """Create a Promotion CR moving `freight` into `stage`.

        `freight` may be the Freight NAME or its ALIAS (resolved here so
        agents can speak in human aliases like 'lively-mongoose').
        """
        if blocked := self._mutation_blocked():
            return blocked
        if not (project and stage and freight):
            return err("kargo: promote needs project, stage and freight")
        resolved = await self._resolve_freight(project, freight)
        if not resolved:
            return err(f"kargo: freight {freight!r} not found in project {project!r} (by name or alias)")
        manifest = (
            f"apiVersion: {_GROUP}/v1alpha1\n"
            "kind: Promotion\n"
            "metadata:\n"
            f"  generateName: {stage}-\n"
            f"  namespace: {project}\n"
            "  labels:\n"
            "    devai.io/initiated-by: devai-agent\n"
            "spec:\n"
            f"  stage: {stage}\n"
            f"  freight: {resolved}\n"
        )
        try:
            out = await kubectl("create", "-f", "-", stdin=manifest, cluster=self._cluster)
            promo = out.split()[0].split("/")[-1] if out else ""
            logger.warning("gitops/kargo: PROMOTION %s created — %s/%s ← freight %s", promo, project, stage, resolved)
            return {"ok": True, "promotion": promo, "project": project, "stage": stage, "freight": resolved}
        except Exception as e:  # noqa: BLE001
            return err(f"kargo: promotion create failed: {e}")

    async def _resolve_freight(self, project: str, ref: str) -> str:
        """Freight name from a name-or-alias reference; "" when not found."""
        try:
            await kubectl_json("get", f"freights.{_GROUP}", ref, "-n", project, cluster=self._cluster)
            return ref
        except Exception:  # noqa: BLE001 — not a name; try alias
            pass
        for f in await self.list_freight(project, limit=100):
            if f.get("alias") == ref:
                return str(f.get("name", ""))
        return ""

    async def list_promotions(self, project: str, stage: str = "", *, limit: int = 10) -> list[dict[str, Any]]:
        if not project:
            return [err("kargo: a project (namespace) is required to list promotions")]
        args = ["get", f"promotions.{_GROUP}", "-n", project, "--sort-by=.metadata.creationTimestamp"]
        try:
            result = await kubectl_json(*args, cluster=self._cluster)
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo: cannot list promotions in {project!r}: {e}")]
        promos = []
        for p in result.get("items", []):
            if stage and p.get("spec", {}).get("stage") != stage:
                continue
            promos.append(
                {
                    "name": p.get("metadata", {}).get("name", ""),
                    "stage": p.get("spec", {}).get("stage", ""),
                    "freight": p.get("spec", {}).get("freight", ""),
                    "phase": p.get("status", {}).get("phase", ""),
                    "message": (p.get("status", {}).get("message") or "")[:300],
                    "created": p.get("metadata", {}).get("creationTimestamp", ""),
                }
            )
        return promos[-limit:]

    # -- common surface mapped onto promotion ------------------------------------

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        """Promote the NEWEST freight in the project into stage `name`."""
        project = self._project(scope)
        if not project:
            return err("kargo: a project (namespace) is required — pass scope/project")
        freight = await self.list_freight(project, limit=1)
        if not freight or not freight[-1].get("name"):
            return err(f"kargo: no freight available in project {project!r}")
        return await self.promote(project, name, str(freight[-1]["name"]))

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        return await self.list_promotions(self._project(scope), stage=name)

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        project = self._project(scope)
        if not revision:
            return err("kargo: rollback needs the freight name/alias to return to — list freight or history first")
        return await self.promote(project, name, revision)


__all__ = ["KargoGitOpsAdapter"]
