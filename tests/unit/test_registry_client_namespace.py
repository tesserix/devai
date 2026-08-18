"""RegistryClient must scope catalog reads to the tenant namespace — aregistry
returns an empty list for an unscoped /v0/skills, so the seeded catalog only
shows up when ?namespace= is passed."""

from __future__ import annotations

import httpx

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


def test_artifact_envelope_uses_scoped_encoded_path(monkeypatch) -> None:
    requested: list[str] = []

    def fake_get(url: str, **_: object) -> httpx.Response:
        requested.append(url)
        return httpx.Response(
            200,
            json={"metadata": {"name": "acme/files", "labels": {"owner": "alice"}}},
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = RegistryClient(base_url="http://reg:12121", namespace="tenant a")

    result = client.get_artifact_envelope("mcp-servers", "acme/files")

    assert result == {"metadata": {"name": "acme/files", "labels": {"owner": "alice"}}}
    assert requested == ["http://reg:12121/v0/servers/acme%2Ffiles?namespace=tenant%20a"]


def test_artifact_envelope_returns_none_only_for_404(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(404))
    client = RegistryClient(base_url="http://reg:12121", namespace="devai")

    assert client.get_artifact_envelope("agents", "missing") is None


def test_list_projects_metadata_visibility_for_authorization(monkeypatch) -> None:
    envelope = {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "private-agent", "visibility": "private"},
        "spec": {"description": "private"},
    }
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(200, json=[envelope]),
    )
    client = RegistryClient(base_url="http://reg:12121", namespace="devai")

    agents = client.list_agents()

    assert agents[0].raw["visibility"] == "private"
