"""Contract tests for the GitOps adapter family (argocd / kargo / flux / noop).

Backends are exercised against faked kubectl output — proving the swap is
real: same surface, same degrade-to-dict error shape, same mutation gate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from devai.adapters.gitops import (
    KNOWN_PROVIDERS,
    GitOpsAdapter,
    NoopGitOpsAdapter,
    create_gitops_adapter,
    create_gitops_adapters,
)
from devai.adapters.gitops import flux as flux_mod
from devai.adapters.gitops import kargo as kargo_mod


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "gitops_provider": "argocd",
        "gitops_mcp_providers": "argocd,kargo,flux",
        "gitops_mutations_enabled": True,
        "kargo_default_project": "",
        "flux_namespace": "flux-system",
        "argocd_namespace": "argocd",
        "argocd_sync_timeout": 5,
        "argocd_health_timeout": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── factory ────────────────────────────────────────────────────────────────


def test_factory_unknown_provider_degrades_to_noop():
    adapter = create_gitops_adapter(_settings(), provider="bogus")
    assert isinstance(adapter, NoopGitOpsAdapter)


def test_factory_known_providers():
    assert set(KNOWN_PROVIDERS) == {"noop", "argocd", "kargo", "flux"}
    for name in KNOWN_PROVIDERS:
        adapter = create_gitops_adapter(_settings(), provider=name)
        assert isinstance(adapter, GitOpsAdapter)
        assert adapter.provider == name


def test_factory_builds_all_mcp_providers():
    adapters = create_gitops_adapters(_settings())
    assert set(adapters) == {"argocd", "kargo", "flux"}


def test_factory_mcp_providers_subset():
    adapters = create_gitops_adapters(_settings(gitops_mcp_providers="flux"))
    assert set(adapters) == {"flux"}


# ── noop contract ──────────────────────────────────────────────────────────


async def test_noop_satisfies_surface():
    noop = NoopGitOpsAdapter()
    assert (await noop.health_check())["ok"] is True
    assert await noop.list_targets() == []
    assert (await noop.get_target("x"))["ok"] is False
    assert (await noop.sync("x"))["ok"] is False
    assert await noop.history("x") == []
    assert (await noop.rollback("x"))["ok"] is False


# ── kargo (faked kubectl) ──────────────────────────────────────────────────


def _kargo_freight(name: str, alias: str, ts: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "creationTimestamp": ts},
        "alias": alias,
        "origin": {"name": "warehouse"},
        "images": [{"repoURL": "ghcr.io/x/app", "tag": "1.2.3"}],
        "commits": [{"repoURL": "https://github.com/x/app", "id": "abcdef1234567890"}],
        "charts": [],
    }


@pytest.fixture
def kargo(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_json(*args: str, timeout: int = 30, cluster=None) -> dict[str, Any]:
        calls.append(args)
        if args[1].startswith("freights"):
            if not args[2].startswith("-"):  # named get: kubectl get freights <name> -n <ns>
                if args[2] == "fr-1":
                    return _kargo_freight("fr-1", "lively-mongoose", "2026-01-01T00:00:00Z")
                raise RuntimeError("not found")
            return {
                "items": [
                    _kargo_freight("fr-0", "old-otter", "2025-12-01T00:00:00Z"),
                    _kargo_freight("fr-1", "lively-mongoose", "2026-01-01T00:00:00Z"),
                ]
            }
        if args[1].startswith("stages"):
            return {
                "items": [
                    {
                        "metadata": {"name": "staging", "namespace": "demo"},
                        "status": {
                            "phase": "Steady",
                            "health": {"status": "Healthy"},
                            "freightHistory": [{"items": {"warehouse": {"name": "fr-1"}}}],
                            "lastPromotion": {"name": "staging-1"},
                        },
                    }
                ]
            }
        return {"items": []}

    created: list[str] = []

    async def fake_kubectl(*args: str, timeout: int = 30, stdin: str = "", cluster=None) -> str:
        if args[0] == "create":
            created.append(stdin)
            return "promotion.kargo.akuity.io/staging-xyz12 created"
        return ""

    monkeypatch.setattr(kargo_mod, "kubectl_json", fake_json)
    monkeypatch.setattr(kargo_mod, "kubectl", fake_kubectl)
    adapter = kargo_mod.KargoGitOpsAdapter()
    adapter._created = created  # type: ignore[attr-defined] — test hook
    return adapter


async def test_kargo_list_stages(kargo):
    stages = await kargo.list_targets("demo")
    assert stages[0]["stage"] == "staging"
    assert stages[0]["health"] == "Healthy"
    assert stages[0]["current_freight"] == ["fr-1"]


async def test_kargo_promote_by_name(kargo):
    result = await kargo.promote("demo", "staging", "fr-1")
    assert result["ok"] is True
    assert result["promotion"] == "staging-xyz12"
    assert "freight: fr-1" in kargo._created[0]


async def test_kargo_promote_resolves_alias(kargo):
    result = await kargo.promote("demo", "staging", "old-otter")
    assert result["ok"] is True
    assert result["freight"] == "fr-0"


async def test_kargo_requires_project():
    adapter = kargo_mod.KargoGitOpsAdapter()
    result = await adapter.get_target("staging")
    assert result["ok"] is False
    assert "project" in result["error"]


async def test_kargo_mutation_gate(kargo):
    gated = kargo_mod.KargoGitOpsAdapter(mutations_enabled=False)
    result = await gated.promote("demo", "staging", "fr-1")
    assert result["ok"] is False
    assert "disabled" in result["error"]


# ── flux (faked kubectl) ───────────────────────────────────────────────────


@pytest.fixture
def flux(monkeypatch):
    annotated: list[tuple[str, ...]] = []

    async def fake_json(*args: str, timeout: int = 30, cluster=None) -> dict[str, Any]:
        if args[1].startswith("kustomizations"):
            obj = {
                "metadata": {"name": "devai-api", "namespace": "devai"},
                "spec": {"suspend": False, "sourceRef": {"kind": "GitRepository", "name": "tesserix-k8s"}},
                "status": {
                    "lastAppliedRevision": "main@sha1:abc123",
                    "conditions": [{"type": "Ready", "status": "True", "message": "Applied revision main@sha1:abc123"}],
                },
            }
            return obj if len(args) > 2 and args[2] == "devai-api" else {"items": [obj]}
        raise RuntimeError("no such resource")

    async def fake_kubectl(*args: str, timeout: int = 30, stdin: str = "", cluster=None) -> str:
        annotated.append(args)
        return ""

    monkeypatch.setattr(flux_mod, "kubectl_json", fake_json)
    monkeypatch.setattr(flux_mod, "kubectl", fake_kubectl)
    adapter = flux_mod.FluxGitOpsAdapter()
    adapter._annotated = annotated  # type: ignore[attr-defined] — test hook
    return adapter


async def test_flux_list_and_detail(flux):
    rows = await flux.list_kustomizations("devai")
    assert rows[0]["name"] == "devai-api"
    assert rows[0]["ready"] == "True"
    detail = await flux.get_target("devai-api", "devai")
    assert detail["revision"] == "main@sha1:abc123"
    assert detail["source"]["name"] == "tesserix-k8s"


async def test_flux_reconcile_annotates(flux):
    result = await flux.reconcile("devai-api", namespace="devai")
    assert result["ok"] is True
    assert any("reconcile.fluxcd.io/requestedAt" in " ".join(c) for c in flux._annotated)


async def test_flux_rollback_is_git_driven(flux):
    result = await flux.rollback("devai-api")
    assert result["ok"] is False
    assert "git revert" in result["error"]


async def test_flux_unknown_kind(flux):
    result = await flux.reconcile("x", kind="wat")
    assert result["ok"] is False


async def test_flux_mutation_gate():
    gated = flux_mod.FluxGitOpsAdapter(mutations_enabled=False)
    result = await gated.set_suspended("devai-api", True)
    assert result["ok"] is False
    assert "disabled" in result["error"]


# ── tools registration + MCP domain ────────────────────────────────────────

EXPECTED_TOOLS = {
    "argocd_list_apps",
    "argocd_get_app",
    "argocd_sync",
    "argocd_wait_healthy",
    "argocd_app_history",
    "argocd_rollback",
    "kargo_list_projects",
    "kargo_list_stages",
    "kargo_get_stage",
    "kargo_list_freight",
    "kargo_promote",
    "kargo_list_promotions",
    "flux_list_kustomizations",
    "flux_list_helmreleases",
    "flux_get_object",
    "flux_list_sources",
    "flux_reconcile",
    "flux_suspend",
}


def test_gitops_tools_registered():
    from devai.tools import registry as tool_registry

    assert set(tool_registry.known()) >= EXPECTED_TOOLS


async def test_gitops_tool_handler_answers_not_raises(monkeypatch):
    """A tool call against a noop backend answers with a readable error string."""
    from devai.tools import gitops_tools
    from devai.tools import registry as tool_registry

    monkeypatch.setitem(gitops_tools._ADAPTERS, "kargo", NoopGitOpsAdapter())
    bound = tool_registry.bind(["kargo_list_stages"], tool_registry.ToolContext(agent_name="t"))
    out = await bound[0].handler({"project": "demo"})
    assert json.loads(out) == []  # noop lists nothing, never raises


def test_mcp_gitops_domain_builds():
    from devai.mcphub.tool_server import gitops_domain

    tools = gitops_domain(_settings())
    assert {t.name for t in tools} == EXPECTED_TOOLS
