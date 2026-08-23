"""RegistryClient must scope catalog reads to the tenant namespace — aregistry
returns an empty list for an unscoped /v0/skills, so the seeded catalog only
shows up when ?namespace= is passed."""

from __future__ import annotations

import httpx
import pytest

from devai.registry.client import RegistryClient, RegistryError


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
        "metadata": {
            "name": "private-agent",
            "visibility": "private",
            "labels": {"devai.tesserix.app/lifecycle": "published"},
            "annotations": {"devai.tesserix.app/eval-run-id": "eval-1"},
        },
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
    assert agents[0].labels["devai.tesserix.app/lifecycle"] == "published"
    assert agents[0].annotations["devai.tesserix.app/eval-run-id"] == "eval-1"


def test_list_datasets_and_eval_suites_preserves_versioned_eval_contract(monkeypatch) -> None:
    def request(method: str, url: str, **_: object) -> httpx.Response:
        if "/datasets" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "kind": "Dataset",
                        "metadata": {"name": "golden", "tag": "3"},
                        "spec": {"description": "Golden cases", "cases": [{"id": "refund"}]},
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "kind": "EvalSuite",
                    "metadata": {"name": "release-gate", "tag": "2"},
                    "spec": {
                        "description": "Release gate",
                        "datasetRef": {"ref": "golden", "version": "3"},
                        "scorers": ["exact_match", "tool_trajectory"],
                        "thresholds": {"success": 0.95},
                    },
                }
            ],
        )

    monkeypatch.setattr(httpx, "request", request)
    client = RegistryClient(base_url="http://reg:12121", namespace="devai")

    dataset = client.list_datasets()[0]
    suite = client.list_eval_suites()[0]

    assert (dataset.name, dataset.version, dataset.case_count) == ("golden", "3", 1)
    assert suite.dataset_ref == {"ref": "golden", "version": "3"}
    assert suite.scorers == ["exact_match", "tool_trajectory"]
    assert suite.thresholds == {"success": 0.95}


def test_eval_publish_accepts_full_manifest_without_double_enveloping(monkeypatch) -> None:
    bodies: list[dict[str, object]] = []

    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        del method, url
        import json

        bodies.append(json.loads(str(kwargs["content"])))
        return httpx.Response(201, json={})

    monkeypatch.setattr(httpx, "request", request)
    client = RegistryClient(base_url="http://reg:12121", namespace="devai")
    manifest = {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Dataset",
        "metadata": {"name": "golden", "tag": "3", "namespace": "devai"},
        "spec": {"cases": []},
    }

    client.publish_dataset(manifest)

    assert bodies == [manifest]


def test_resolve_agent_returns_composition_and_scopes_the_request(monkeypatch) -> None:
    requested: list[str] = []

    def request(method: str, url: str, **_: object) -> httpx.Response:
        assert method == "GET"
        requested.append(url)
        return httpx.Response(
            200,
            json={
                "agent": {
                    "kind": "Agent",
                    "metadata": {
                        "name": "requirements-analyst-agent",
                        "tag": "1.0.0",
                        "labels": {"ai.tesserix.dev/runtime": "tesserix-adk"},
                    },
                    "spec": {"skills": ["requirements-analyst"]},
                },
                "resolved": {
                    "skills": [
                        {
                            "kind": "Skill",
                            "metadata": {"name": "requirements-analyst"},
                            "spec": {"tools": ["read_repository"]},
                        }
                    ],
                    "prompts": [
                        {
                            "kind": "Prompt",
                            "metadata": {"name": "requirements-analyst-prompt-v1"},
                            "spec": {"systemPrompt": "Review requirements."},
                        }
                    ],
                },
                "unresolved": [],
            },
        )

    monkeypatch.setattr(httpx, "request", request)
    client = RegistryClient(base_url="http://reg:12121", namespace="tenant a")

    result = client.resolve_agent("requirements/analyst-agent")

    assert result.agent.name == "requirements-analyst-agent"
    assert result.resolved["skills"][0]["metadata"]["name"] == "requirements-analyst"
    assert result.unresolved == []
    assert requested == ["http://reg:12121/v0/agents/requirements%2Fanalyst-agent/resolved?namespace=tenant%20a"]


def test_registry_http_error_does_not_expose_upstream_body(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: httpx.Response(
            503,
            text="internal hostname registry-db.prod plus bearer-secret",
        ),
    )
    client = RegistryClient(base_url="http://reg:12121", namespace="devai")

    with pytest.raises(RegistryError) as caught:
        client.resolve_agent("requirements-analyst-agent")

    message = str(caught.value)
    assert message == ("registry: 503 on GET /v0/agents/requirements-analyst-agent/resolved?namespace=devai")
    assert "bearer-secret" not in message
