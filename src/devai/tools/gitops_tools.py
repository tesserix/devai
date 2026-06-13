"""GitOps tools — Argo CD, Kargo and Flux CD operations for agents.

Registered into `devai.tools.registry` so any specialization can opt in via
`allowed_tools: [argocd_sync, kargo_promote, flux_reconcile, ...]`, and
served ad-hoc through the gitops MCP domain (`/mcp/gitops`).

Execution routes through the `adapters.gitops` family — each handler builds
(and caches) the right backend lazily from settings, so importing this
module costs nothing and a missing controller degrades into a readable
error string the agent can act on, never an exception.

Mutating tools (sync / promote / rollback / reconcile / suspend) honor the
platform gate `DEVAI_GITOPS_MUTATIONS_ENABLED` and are logged at WARNING
with run attribution for the audit trail.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.tools.registry import Handler, ToolContext, register

logger = logging.getLogger(__name__)

_ADAPTERS: dict[str, Any] = {}


def _adapter(provider: str) -> Any:
    """Build-once cache per provider; the factory never raises."""
    if provider not in _ADAPTERS:
        from devai.adapters.gitops import create_gitops_adapter
        from devai.config import settings

        _ADAPTERS[provider] = create_gitops_adapter(settings, provider=provider)
    return _ADAPTERS[provider]


def reset_adapters() -> None:
    """Test hook — drop the cache so settings changes take effect."""
    _ADAPTERS.clear()


def _dump(result: Any) -> str:
    try:
        return json.dumps(result, indent=2, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def _audit(ctx: ToolContext, tool: str, args: dict[str, Any]) -> None:
    logger.warning(
        "gitops tool %s by agent=%s run=%s user=%s args=%s",
        tool,
        ctx.agent_name or "?",
        ctx.run_id or "?",
        ctx.triggered_by or "?",
        {k: v for k, v in args.items() if k != "_tool"},
    )


def _adapter_for_cluster(provider: str, cluster: dict[str, Any]) -> Any:
    """A FRESH adapter targeting a user-connected cluster (never cached —
    credentials are per-user and per-call)."""
    from devai.config import settings

    mutations = bool(getattr(settings, "gitops_mutations_enabled", True))
    if provider == "argocd":
        from devai.adapters.gitops.argocd import ArgoCDGitOpsAdapter

        return ArgoCDGitOpsAdapter(settings, mutations_enabled=mutations, cluster=cluster)
    if provider == "kargo":
        from devai.adapters.gitops.kargo import KargoGitOpsAdapter

        return KargoGitOpsAdapter(mutations_enabled=mutations, cluster=cluster)
    if provider == "flux":
        from devai.adapters.gitops.flux import FluxGitOpsAdapter

        return FluxGitOpsAdapter(mutations_enabled=mutations, cluster=cluster)
    return _adapter(provider)


async def _resolve_cluster(ctx: ToolContext, name: str) -> tuple[dict[str, Any] | None, str]:
    """The caller's connected cluster by name → (cluster, "") or (None, error)."""
    email = (ctx.triggered_by or "").strip()
    if "@" not in email:
        return None, (
            "ERROR: this call carries no user identity, so a personal cluster cannot be "
            "resolved — omit 'cluster' to use the platform cluster"
        )
    from devai.settings.connections import user_cluster, user_cluster_names

    cluster = await user_cluster(email, name)
    if cluster is None or not cluster.get("server"):
        names = await user_cluster_names(email)
        return None, (
            f"ERROR: no connected cluster named {name!r} for {email}. "
            + (f"Available: {', '.join(names)}" if names else "Connect one in Settings → Kubernetes Cluster.")
        )
    return cluster, ""


def _make(provider: str, method: str, *, mutating: bool = False, arg_map: dict[str, str] | None = None):
    """Factory-of-factories: one handler shape for every gitops tool.

    `arg_map` renames tool-call args to adapter kwargs (e.g. project→scope).
    Every tool accepts an optional `cluster` — the name of one of the
    CALLER's connected Kubernetes clusters (Settings → Kubernetes Cluster);
    omitted = the platform cluster.
    """

    def factory(ctx: ToolContext) -> Handler:
        async def handler(args: dict[str, Any]) -> str:
            cluster_name = str(args.pop("cluster", "") or "").strip()
            if cluster_name:
                cluster, problem = await _resolve_cluster(ctx, cluster_name)
                if problem:
                    return problem
                adapter = _adapter_for_cluster(provider, cluster)
            else:
                adapter = _adapter(provider)
            fn = getattr(adapter, method, None)
            if fn is None:
                return f"ERROR: {provider} backend does not support {method}"
            kwargs = {}
            for key, value in args.items():
                if key.startswith("_") or value in (None, ""):
                    continue
                kwargs[(arg_map or {}).get(key, key)] = value
            if mutating:
                _audit(ctx, f"{provider}.{method}", kwargs)
            try:
                result = await fn(**kwargs)
            except TypeError as e:
                return f"ERROR: bad arguments for {provider}.{method}: {e}"
            except Exception as e:  # noqa: BLE001 — tools answer, never raise
                return f"ERROR: {provider}.{method} failed: {e}"
            return _dump(result)

        return handler

    return factory


_CLUSTER = {
    "type": "string",
    "description": (
        "Optional — the name of one of YOUR connected Kubernetes clusters "
        "(Settings → Kubernetes Cluster). Omit to use the platform cluster."
    ),
}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    # Every gitops tool can target a user-connected cluster via `cluster`.
    schema: dict[str, Any] = {"type": "object", "properties": {**props, "cluster": _CLUSTER}}
    if required:
        schema["required"] = required
    return schema


_NAME = {"type": "string", "description": "Target name"}
_PROJECT = {"type": "string", "description": "Kargo project (its namespace)"}
_NS = {"type": "string", "description": "Kubernetes namespace (blank = default)"}
_FLUX_KIND = {
    "type": "string",
    "description": "kustomization | helmrelease | gitrepository | helmrepository | ocirepository",
}


def register_gitops_tools(*, overwrite: bool = False) -> None:
    """Register the full Argo CD + Kargo + Flux tool set. Safe to call repeatedly."""

    # ── Argo CD ──────────────────────────────────────────────────────────
    register(
        "argocd_list_apps",
        "List Argo CD Applications with sync + health status. Optionally filter by Argo CD project.",
        _obj({"project": {"type": "string", "description": "Argo CD project filter (optional)"}}),
        _make("argocd", "list_targets", arg_map={"project": "scope"}),
        overwrite=overwrite,
    )
    register(
        "argocd_get_app",
        "Sync status, health, revision, recent conditions and source repo/path of one Argo CD Application.",
        _obj({"name": _NAME}, ["name"]),
        _make("argocd", "get_target"),
        overwrite=overwrite,
    )
    register(
        "argocd_sync",
        "Trigger a sync of an Argo CD Application (converge cluster to Git). Mutating — gated and audited.",
        _obj({"name": _NAME}, ["name"]),
        _make("argocd", "sync", mutating=True),
        overwrite=overwrite,
    )
    register(
        "argocd_wait_healthy",
        "Poll an Argo CD Application until Synced+Healthy, Degraded, Failed or timeout. Use after argocd_sync.",
        _obj(
            {"name": _NAME, "timeout": {"type": "integer", "description": "Max seconds to wait (default 300)"}},
            ["name"],
        ),
        _make("argocd", "wait_for_sync"),
        overwrite=overwrite,
    )
    register(
        "argocd_app_history",
        "Deployment history (revision ids + timestamps) of an Argo CD Application — needed before rollback.",
        _obj({"name": _NAME}, ["name"]),
        _make("argocd", "history"),
        overwrite=overwrite,
    )
    register(
        "argocd_rollback",
        "Roll an Argo CD Application back to a previous revision (history id from argocd_app_history; "
        "blank = previous deployment). Mutating — gated and audited.",
        _obj(
            {"name": _NAME, "revision": {"type": "string", "description": "History id to return to (optional)"}},
            ["name"],
        ),
        _make("argocd", "rollback", mutating=True),
        overwrite=overwrite,
    )

    # ── Kargo ────────────────────────────────────────────────────────────
    register(
        "kargo_list_projects",
        "List Kargo projects (delivery pipelines).",
        _obj({}),
        _make("kargo", "list_projects"),
        overwrite=overwrite,
    )
    register(
        "kargo_list_stages",
        "List Kargo stages (dev/staging/prod) with phase, health and current freight. "
        "Omit project to list across all projects.",
        _obj({"project": _PROJECT}),
        _make("kargo", "list_targets", arg_map={"project": "scope"}),
        overwrite=overwrite,
    )
    register(
        "kargo_get_stage",
        "Detail for one Kargo stage: phase, health, requested freight sources and last promotion outcome.",
        _obj({"project": _PROJECT, "stage": _NAME}, ["project", "stage"]),
        _make("kargo", "get_target", arg_map={"project": "scope", "stage": "name"}),
        overwrite=overwrite,
    )
    register(
        "kargo_list_freight",
        "List freight (promotable artifact sets: images, charts, commits) in a Kargo project, oldest→newest.",
        _obj({"project": _PROJECT, "limit": {"type": "integer", "description": "Max entries (default 20)"}}, ["project"]),
        _make("kargo", "list_freight"),
        overwrite=overwrite,
    )
    register(
        "kargo_promote",
        "Promote a freight (by name or alias) into a Kargo stage — THE release action. Mutating — gated and audited.",
        _obj({"project": _PROJECT, "stage": _NAME, "freight": {"type": "string", "description": "Freight name or alias"}}, ["project", "stage", "freight"]),
        _make("kargo", "promote", mutating=True),
        overwrite=overwrite,
    )
    register(
        "kargo_list_promotions",
        "Recent promotions of a Kargo project (optionally one stage) with phase + message — the release history.",
        _obj({"project": _PROJECT, "stage": {"type": "string", "description": "Stage filter (optional)"}}, ["project"]),
        _make("kargo", "list_promotions"),
        overwrite=overwrite,
    )

    # ── Flux CD ──────────────────────────────────────────────────────────
    register(
        "flux_list_kustomizations",
        "List Flux Kustomizations with Ready condition, suspension and last applied revision.",
        _obj({"namespace": _NS}),
        _make("flux", "list_kustomizations"),
        overwrite=overwrite,
    )
    register(
        "flux_list_helmreleases",
        "List Flux HelmReleases with Ready condition, suspension and last applied revision.",
        _obj({"namespace": _NS}),
        _make("flux", "list_helmreleases"),
        overwrite=overwrite,
    )
    register(
        "flux_get_object",
        "Detail for one Flux Kustomization/HelmRelease: conditions, source ref, revision, suspension.",
        _obj({"name": _NAME, "namespace": _NS, "kind": _FLUX_KIND}, ["name"]),
        _make("flux", "get_target", arg_map={"namespace": "scope"}),
        overwrite=overwrite,
    )
    register(
        "flux_list_sources",
        "List Flux sources (GitRepositories, HelmRepositories, OCIRepositories) with readiness.",
        _obj({"namespace": _NS}),
        _make("flux", "list_sources"),
        overwrite=overwrite,
    )
    register(
        "flux_reconcile",
        "Request an immediate Flux reconcile of a Kustomization/HelmRelease/source (what `flux reconcile` does). "
        "Mutating — gated and audited.",
        _obj({"name": _NAME, "namespace": _NS, "kind": _FLUX_KIND}, ["name"]),
        _make("flux", "reconcile", mutating=True),
        overwrite=overwrite,
    )
    register(
        "flux_suspend",
        "Suspend or resume Flux reconciliation of an object (suspended=true freezes it — e.g. during an incident). "
        "Mutating — gated and audited.",
        _obj(
            {
                "name": _NAME,
                "suspended": {"type": "boolean", "description": "true = suspend, false = resume"},
                "namespace": _NS,
                "kind": _FLUX_KIND,
            },
            ["name", "suspended"],
        ),
        _make("flux", "set_suspended", mutating=True),
        overwrite=overwrite,
    )


# Self-register at import (mirrors web/shell/checkpoint tool families).
register_gitops_tools()

__all__ = ["register_gitops_tools", "reset_adapters"]
