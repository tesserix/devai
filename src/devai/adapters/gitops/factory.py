"""Factory — pick the GitOps backend(s) at runtime.

`create_gitops_adapter(settings)` reads `settings.gitops_provider`
(env: `DEVAI_GITOPS_PROVIDER`, default `argocd`) and returns one of:

    noop   → NoopGitOpsAdapter
    argocd → ArgoCDGitOpsAdapter
    kargo  → KargoGitOpsAdapter
    flux   → FluxGitOpsAdapter

`create_gitops_adapters(settings)` builds EVERY provider named in
`settings.gitops_mcp_providers` (comma list) — the gitops MCP domain
exposes them side by side, each degrading independently.

Graceful degradation: unknown provider or a constructor failure logs and
returns Noop. The pod degrades, it never crashes.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import AdapterRegistry
from devai.adapters.gitops.base import GitOpsAdapter
from devai.adapters.gitops.noop import NoopGitOpsAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "argocd", "kargo", "flux")


def _mutations(settings: Any) -> bool:
    return bool(getattr(settings, "gitops_mutations_enabled", True))


def _build_noop(settings: Any) -> GitOpsAdapter:
    return NoopGitOpsAdapter()


def _build_argocd(settings: Any) -> GitOpsAdapter:
    from devai.adapters.gitops.argocd import ArgoCDGitOpsAdapter

    return ArgoCDGitOpsAdapter(settings, mutations_enabled=_mutations(settings))


def _build_kargo(settings: Any) -> GitOpsAdapter:
    from devai.adapters.gitops.kargo import KargoGitOpsAdapter

    return KargoGitOpsAdapter(
        default_project=str(getattr(settings, "kargo_default_project", "") or ""),
        mutations_enabled=_mutations(settings),
    )


def _build_flux(settings: Any) -> GitOpsAdapter:
    from devai.adapters.gitops.flux import FluxGitOpsAdapter

    return FluxGitOpsAdapter(
        default_namespace=str(getattr(settings, "flux_namespace", "flux-system") or "flux-system"),
        mutations_enabled=_mutations(settings),
    )


registry: AdapterRegistry[GitOpsAdapter] = AdapterRegistry("gitops")
registry.register("noop", _build_noop)
registry.register("argocd", _build_argocd)
registry.register("kargo", _build_kargo)
registry.register("flux", _build_flux)


def create_gitops_adapter(settings: Any, provider: str = "") -> GitOpsAdapter:
    """One backend per the provider (argument > settings.gitops_provider). Never raises."""
    name = (provider or getattr(settings, "gitops_provider", "") or "argocd").strip().lower()
    if not registry.has(name):
        logger.warning("gitops: unknown provider %r (known: %s) — using noop", name, ", ".join(KNOWN_PROVIDERS))
        return NoopGitOpsAdapter()
    try:
        adapter = registry.resolve(name, settings)
        logger.info("gitops: adapter ready (provider=%s)", name)
        return adapter
    except Exception:  # noqa: BLE001 — degrade, don't crash
        logger.warning("gitops: provider %r failed to build — using noop", name, exc_info=True)
        return NoopGitOpsAdapter()


def create_gitops_adapters(settings: Any) -> dict[str, GitOpsAdapter]:
    """Every provider in settings.gitops_mcp_providers, keyed by name.

    Noop results for non-noop requests are dropped — a backend that can't
    build shouldn't advertise tools it will only refuse.
    """
    raw = str(getattr(settings, "gitops_mcp_providers", "") or "argocd,kargo,flux")
    out: dict[str, GitOpsAdapter] = {}
    for name in (p.strip().lower() for p in raw.split(",")):
        if not name or name in out:
            continue
        adapter = create_gitops_adapter(settings, provider=name)
        if name != "noop" and adapter.provider == "noop":
            continue
        out[name] = adapter
    return out


__all__ = ["KNOWN_PROVIDERS", "create_gitops_adapter", "create_gitops_adapters", "registry"]
