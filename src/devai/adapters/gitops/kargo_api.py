"""Kargo via its Connect-RPC API — for external Kargo control planes.

Kargo's API server speaks Connect-RPC: a unary call is
``POST https://<host>/akuity.io.kargo.service.v1alpha1.KargoService/<Method>``
with ``Content-Type: application/json``, the request message as the JSON body,
and the response message as JSON. Auth is ``Authorization: Bearer <token>``.

This is the API-mode counterpart to the kubectl-against-a-cluster Kargo
backend, for when a user connected Kargo by API URL + token (Settings → Kargo,
mode ``api``) instead of a cluster. Field shapes follow Kargo's documented
v1alpha1 messages; parsing is defensive so a minor server-version difference
degrades a field to empty rather than raising. The kubectl path stays primary.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.gitops.base import GitOpsAdapter, err

logger = logging.getLogger(__name__)

_SERVICE = "akuity.io.kargo.service.v1alpha1.KargoService"


class KargoApiAdapter(GitOpsAdapter):
    provider = "kargo"

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        default_project: str = "",
        mutations_enabled: bool = True,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(mutations_enabled=mutations_enabled)
        self._base = api_url.rstrip("/")
        self._token = token
        self.default_project = default_project
        self._verify = verify_tls
        self._timeout = timeout

    def _project(self, scope: str) -> str:
        return scope or self.default_project

    async def _rpc(self, method: str, message: dict[str, Any]) -> dict[str, Any]:
        import httpx  # lazy

        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        url = f"{self._base}/{_SERVICE}/{method}"
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=message)
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def list_projects(self) -> list[dict[str, Any]]:
        try:
            data = await self._rpc("ListProjects", {})
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo-api: list projects failed: {e}")]
        return [{"name": (p.get("metadata") or {}).get("name", "")} for p in data.get("projects", []) or []]

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        project = self._project(scope)
        if not project:
            return [err("kargo-api: a project is required — pass scope/project")]
        try:
            data = await self._rpc("ListStages", {"project": project})
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo-api: list stages failed: {e}")]
        out = []
        for s in data.get("stages", []) or []:
            status = s.get("status", {}) or {}
            out.append(
                {
                    "stage": (s.get("metadata") or {}).get("name", ""),
                    "project": project,
                    "phase": status.get("phase", ""),
                    "health": (status.get("health") or {}).get("status", "Unknown"),
                    "last_promotion": (status.get("lastPromotion") or {}).get("name", ""),
                }
            )
        return out

    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        project = self._project(scope)
        if not project:
            return err("kargo-api: a project is required — pass scope/project")
        try:
            data = await self._rpc("GetStage", {"project": project, "name": name})
        except Exception as e:  # noqa: BLE001
            return err(f"kargo-api: cannot read stage {name!r}: {e}")
        stage = data.get("stage", data) or {}
        status = stage.get("status", {}) or {}
        return {
            "stage": name,
            "project": project,
            "phase": status.get("phase", ""),
            "health": (status.get("health") or {}).get("status", "Unknown"),
            "last_promotion": (status.get("lastPromotion") or {}).get("name", ""),
        }

    async def list_freight(self, project: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not project:
            return [err("kargo-api: a project is required to query freight")]
        try:
            data = await self._rpc("QueryFreight", {"project": project})
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo-api: query freight failed: {e}")]
        # QueryFreight returns groups{name -> {freight: [...]}}; flatten.
        items: list[dict[str, Any]] = []
        groups = data.get("groups", {}) or {}
        for g in groups.values():
            items.extend(g.get("freight", []) or [])
        items.extend(data.get("freight", []) or [])
        out = []
        for f in items[-limit:]:
            meta = f.get("metadata") or {}
            out.append(
                {
                    "name": meta.get("name", ""),
                    "alias": (meta.get("labels") or {}).get("kargo.akuity.io/alias", ""),
                    "images": [f"{i.get('repoURL', '')}:{i.get('tag', '')}" for i in f.get("images", []) or []],
                }
            )
        return out

    async def promote(self, project: str, stage: str, freight: str) -> dict[str, Any]:
        if blocked := self._mutation_blocked():
            return blocked
        if not (project and stage and freight):
            return err("kargo-api: promote needs project, stage and freight")
        try:
            data = await self._rpc("PromoteToStage", {"project": project, "stage": stage, "freight": freight})
        except Exception as e:  # noqa: BLE001
            return err(f"kargo-api: promotion failed: {e}")
        promo = (data.get("promotion") or {}).get("metadata", {}).get("name", "")
        logger.warning("gitops/kargo-api: PROMOTION %s — %s/%s ← %s", promo, project, stage, freight)
        return {"ok": True, "promotion": promo, "project": project, "stage": stage, "freight": freight}

    async def list_promotions(self, project: str, stage: str = "", *, limit: int = 10) -> list[dict[str, Any]]:
        if not project:
            return [err("kargo-api: a project is required to list promotions")]
        msg: dict[str, Any] = {"project": project}
        if stage:
            msg["stage"] = stage
        try:
            data = await self._rpc("ListPromotions", msg)
        except Exception as e:  # noqa: BLE001
            return [err(f"kargo-api: list promotions failed: {e}")]
        out = []
        for p in (data.get("promotions", []) or [])[-limit:]:
            spec, status = p.get("spec", {}) or {}, p.get("status", {}) or {}
            out.append(
                {
                    "name": (p.get("metadata") or {}).get("name", ""),
                    "stage": spec.get("stage", ""),
                    "freight": spec.get("freight", ""),
                    "phase": status.get("phase", ""),
                }
            )
        return out

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        project = self._project(scope)
        freight = await self.list_freight(project, limit=1)
        if not freight or not freight[-1].get("name"):
            return err(f"kargo-api: no freight available in project {project!r}")
        return await self.promote(project, name, str(freight[-1]["name"]))

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        return await self.list_promotions(self._project(scope), stage=name)

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        if not revision:
            return err("kargo-api: rollback needs the freight name/alias to return to")
        return await self.promote(self._project(scope), name, revision)


__all__ = ["KargoApiAdapter"]
