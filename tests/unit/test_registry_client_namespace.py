"""RegistryClient must scope catalog reads to the tenant namespace — aregistry
returns an empty list for an unscoped /v0/skills, so the seeded catalog only
shows up when ?namespace= is passed."""

from __future__ import annotations

from devai.registry.client import RegistryClient


def test_ns_appends_namespace_query() -> None:
    c = RegistryClient(base_url="http://reg:12121", namespace="devai")
    assert c._ns("/v0/skills") == "/v0/skills?namespace=devai"
    assert c._ns("/v0/agents") == "/v0/agents?namespace=devai"
    # respects an existing query string
    assert c._ns("/v0/skills?q=x") == "/v0/skills?q=x&namespace=devai"


def test_ns_unscoped_when_no_namespace() -> None:
    c = RegistryClient(base_url="http://reg:12121")
    assert c._ns("/v0/skills") == "/v0/skills"


def test_factory_defaults_namespace_to_tenant() -> None:
    from types import SimpleNamespace

    from devai.registry.client import create_registry_client

    s = SimpleNamespace(registry_url="http://reg:12121", registry_default_tenant="devai")
    client = create_registry_client(s)
    assert client is not None
    assert client._namespace == "devai"
