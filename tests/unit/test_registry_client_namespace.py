"""RegistryClient must scope catalog reads to the tenant namespace — aregistry
returns an empty list for an unscoped /v0/skills, so the seeded catalog only
shows up when ?namespace= is passed."""

from __future__ import annotations

from devai.registry.client import RegistryClient


def test_ns_appends_namespace_and_limit() -> None:
    c = RegistryClient(base_url="http://reg:12121", namespace="devai", list_limit=10000)
    assert c._ns("/v0/skills") == "/v0/skills?namespace=devai&limit=10000"
    assert c._ns("/v0/agents") == "/v0/agents?namespace=devai&limit=10000"
    # respects an existing query string
    assert c._ns("/v0/skills?q=x") == "/v0/skills?q=x&namespace=devai&limit=10000"


def test_ns_limit_without_namespace() -> None:
    c = RegistryClient(base_url="http://reg:12121", list_limit=10000)
    assert c._ns("/v0/skills") == "/v0/skills?limit=10000"


def test_factory_defaults_namespace_to_tenant() -> None:
    from types import SimpleNamespace

    from devai.registry.client import create_registry_client

    s = SimpleNamespace(registry_url="http://reg:12121", registry_default_tenant="devai")
    client = create_registry_client(s)
    assert client is not None
    assert client._namespace == "devai"
