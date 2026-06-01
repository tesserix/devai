"""Contract tests for the registry adapter family.

Mirrors tests/unit/test_memory_adapters.py: proves the factory never raises and
that every backend satisfies the RegistryAdapter ABC. The HTTP-backed providers
are exercised for their degradation paths only (no network).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devai.adapters.registry import (
    KNOWN_PROVIDERS,
    RegistryAdapter,
    create_registry_adapter,
    registry_registry,
)
from devai.adapters.registry.noop import NoopRegistryAdapter


def test_all_providers_registered():
    for p in KNOWN_PROVIDERS:
        assert registry_registry.has(p), f"{p} not registered"


def test_unknown_provider_falls_back_to_noop():
    adapter = create_registry_adapter(SimpleNamespace(registry_provider="does-not-exist"))
    assert isinstance(adapter, NoopRegistryAdapter)


def test_tesserix_without_url_degrades_to_noop():
    # Missing registry_url => AdapterNotConfigured => Noop (never raises).
    adapter = create_registry_adapter(SimpleNamespace(registry_provider="tesserix"))
    assert isinstance(adapter, NoopRegistryAdapter)


def test_noop_satisfies_abc_and_is_empty():
    adapter = create_registry_adapter(SimpleNamespace(registry_provider="noop"))
    assert isinstance(adapter, RegistryAdapter)
    assert adapter.list_skills() == []
    assert adapter.list_prompts() == []
    assert adapter.list_mcp_servers() == []
    assert adapter.list_agents() == []
    assert adapter.get_skill("x") is None
    assert adapter.discover("skills", "language=go") == []
    health = asyncio.run(adapter.health_check())
    assert health["ok"] is True and health["provider"] == "noop"


def test_tesserix_builds_with_url_and_exposes_discover():
    adapter = create_registry_adapter(SimpleNamespace(registry_provider="tesserix", registry_url="http://localhost:9"))
    # Built (not Noop), and discover degrades to [] when the endpoint is down.
    assert adapter.provider_name == "tesserix"
    assert adapter.discover("skills", "language=go") == []


@pytest.mark.parametrize("provider", ["mcp_registry", "portkey", "solo_aregistry"])
def test_other_providers_degrade_without_config(provider):
    adapter = create_registry_adapter(SimpleNamespace(registry_provider=provider))
    # No config for these => Noop fallback, never an exception.
    assert isinstance(adapter, NoopRegistryAdapter)
