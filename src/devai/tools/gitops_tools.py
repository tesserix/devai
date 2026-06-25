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
    from devai.config import settings

    if bool(getattr(settings, "gitops_require_cluster_ca", False)) and not str(cluster.get("ca_data") or "").strip():
        return None, (
            f"ERROR: cluster {name!r} has no CA certificate and policy "
            "(DEVAI_GITOPS_REQUIRE_CLUSTER_CA) forbids insecure TLS — add the CA cert to the "
            "connector in Settings → Kubernetes Cluster."
        )
    return cluster, ""


async def _resolve_api_adapter(ctx: ToolContext, provider: str, name: str) -> tuple[Any, str]:
    """Build an API-mode adapter for the caller's external Argo CD / Kargo.

    ``mode == "kubectl"`` connectors aren't API-reachable — tell the caller to
    use the ``cluster`` arg instead. Returns (adapter, "") or (None, error).
    """
    email = (ctx.triggered_by or "").strip()
    if "@" not in email:
        return None, f"ERROR: this call carries no user identity, so a personal {provider} cannot be resolved"
    from devai.config import settings

    mutations = bool(getattr(settings, "gitops_mutations_enabled", True))
    if provider == "argocd":
        from devai.settings.connections import user_argocd

        conn = await user_argocd(email, name)
        if conn is None or not conn.get("server_url"):
            return None, f"ERROR: no connected Argo CD named {name!r} for {email} (Settings → Argo CD)."
        if conn.get("mode") == "kubectl":
            return None, f"ERROR: Argo CD {name!r} is kubectl-mode — use the 'cluster' arg with its cluster instead."
        from devai.adapters.gitops.argocd_api import ArgoCDApiAdapter

        return ArgoCDApiAdapter(conn["server_url"], conn.get("token", ""), mutations_enabled=mutations), ""
    if provider == "kargo":
        from devai.settings.connections import user_kargo

        conn = await user_kargo(email, name)
        if conn is None or not conn.get("api_url"):
            return None, f"ERROR: no connected Kargo named {name!r} for {email} (Settings → Kargo)."
        if conn.get("mode") == "kubectl":
            return None, f"ERROR: Kargo {name!r} is kubectl-mode — use the 'cluster' arg with its cluster instead."
        from devai.adapters.gitops.kargo_api import KargoApiAdapter

        return (
            KargoApiAdapter(
                conn["api_url"],
                conn.get("token", ""),
                default_project=conn.get("project", ""),
                mutations_enabled=mutations,
            ),
            "",
        )
    return None, f"ERROR: {provider} has no API mode"


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
            # provider-specific instance arg: argocd_* tools accept `argocd`,
            # kargo_* tools accept `kargo` → a user's external API-mode instance.
            instance_name = str(args.pop(provider, "") or "").strip() if provider in ("argocd", "kargo") else ""
            if instance_name:
                adapter, problem = await _resolve_api_adapter(ctx, provider, instance_name)
                if problem:
                    return problem
            elif cluster_name:
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
_ARGOCD_INSTANCE = {
    "type": "string",
    "description": (
        "Optional — the name of one of YOUR connected Argo CD instances "
        "(Settings → Argo CD, API mode). Targets that managed Argo CD's API directly."
    ),
}
_KARGO_INSTANCE = {
    "type": "string",
    "description": (
        "Optional — the name of one of YOUR connected Kargo control planes "
        "(Settings → Kargo, API mode). Targets that Kargo's API directly."
    ),
}


def _obj(props: dict[str, Any], required: list[str] | None = None, *, instance: str = "") -> dict[str, Any]:
    # Every gitops tool can target a user-connected cluster via `cluster`;
    # argocd_*/kargo_* tools also take an `argocd`/`kargo` API-mode instance.
    extra: dict[str, Any] = {"cluster": _CLUSTER}
    if instance == "argocd":
        extra["argocd"] = _ARGOCD_INSTANCE
    elif instance == "kargo":
        extra["kargo"] = _KARGO_INSTANCE
    schema: dict[str, Any] = {"type": "object", "properties": {**props, **extra}}
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
        _obj(
            {"project": _PROJECT, "limit": {"type": "integer", "description": "Max entries (default 20)"}}, ["project"]
        ),
        _make("kargo", "list_freight"),
        overwrite=overwrite,
    )
    register(
        "kargo_promote",
        "Promote a freight (by name or alias) into a Kargo stage — THE release action. Mutating — gated and audited.",
        _obj(
            {
                "project": _PROJECT,
                "stage": _NAME,
                "freight": {"type": "string", "description": "Freight name or alias"},
            },
            ["project", "stage", "freight"],
        ),
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

    # Expose the API-mode instance arg on the argocd_*/kargo_* tools so the
    # model can target a user's external Argo CD / Kargo (resolved in _make).
    from devai.tools.registry import _REGISTRY  # noqa: PLC0415

    for tool_name, entry in _REGISTRY.items():
        instance = "argocd" if tool_name.startswith("argocd_") else "kargo" if tool_name.startswith("kargo_") else ""
        if not instance:
            continue
        props = entry.spec.parameters.setdefault("properties", {})
        props.setdefault(instance, _ARGOCD_INSTANCE if instance == "argocd" else _KARGO_INSTANCE)


# Self-register at import (mirrors web/shell/checkpoint tool families).
register_gitops_tools()

__all__ = ["register_gitops_tools", "reset_adapters"]
