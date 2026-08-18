"""Object-level tenancy guarantees for the registry proxy."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.registry.client import Agent
from devai.registry.routes import router


def _manifest(name: str, *, labels: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Agent",
        "metadata": {
            "name": name,
            "visibility": "public",
            "labels": labels or {},
        },
        "spec": {
            "title": name,
            "description": "private agent",
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
            "systemPrompt": "Keep this private.",
        },
    }


def _headers(user: str, tenant: str) -> dict[str, str]:
    return {
        "x-forwarded-user": f"{user}@example.com",
        "x-forwarded-uid": user,
        "x-forwarded-tenant": tenant,
    }


class _Registry:
    def __init__(self) -> None:
        self._namespace = "devai"
        self.items: dict[str, dict[str, Any]] = {}

    def list_agents(self) -> list[Agent]:
        return [self._agent(body) for body in self.items.values()]

    def get_agent(self, name: str) -> Agent | None:
        body = self.items.get(name)
        return self._agent(body) if body else None

    def get_artifact_envelope(self, plural: str, name: str) -> dict[str, Any] | None:
        assert plural == "agents"
        body = self.items.get(name)
        return deepcopy(body) if body else None

    def publish_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(body)
        name = copied["metadata"]["name"]
        self.items[name] = copied
        return {"name": name}

    def delete(self, plural: str, name: str) -> None:
        assert plural == "agents"
        self.items.pop(name)

    def refresh(self) -> None:
        return None

    @staticmethod
    def _agent(body: dict[str, Any]) -> Agent:
        metadata = body["metadata"]
        spec = body["spec"]
        labels = dict(metadata.get("labels") or {})
        raw = {
            **spec,
            "name": metadata["name"],
            "labels": labels,
            "visibility": metadata.get("visibility", ""),
        }
        return Agent(
            name=metadata["name"],
            title=spec.get("title", ""),
            description=spec.get("description", ""),
            version=str(metadata.get("tag", "")),
            labels=labels,
            raw=raw,
        )


def _client() -> tuple[TestClient, _Registry]:
    app = FastAPI()
    registry = _Registry()
    app.state.registry_client = registry
    app.state.config = SimpleNamespace(
        auth_bff_shared_secret="",
        trust_forwarded_without_secret=True,
    )
    app.include_router(router)
    return TestClient(app), registry


def test_publish_is_private_and_server_owned() -> None:
    client, registry = _client()

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=_manifest(
            "alice-agent",
            labels={
                "devai.tesserix.app/owner-id": "attacker-chosen",
                "devai.tesserix.app/visibility": "public",
            },
        ),
    )

    assert response.status_code == 201, response.text
    metadata = registry.items["alice-agent"]["metadata"]
    assert metadata["visibility"] == "private"
    assert metadata["labels"]["devai.tesserix.app/visibility"] == "private"
    assert metadata["labels"]["devai.tesserix.app/owner-id"] != "attacker-chosen"


def test_another_tenant_cannot_list_read_or_overwrite_private_agent() -> None:
    client, registry = _client()
    created = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=_manifest("private-agent"),
    )
    assert created.status_code == 201, created.text

    bob = _headers("bob", "tenant-b")
    assert client.get("/api/registry/agents", headers=bob).json() == []
    assert client.get("/api/registry/agents/private-agent", headers=bob).status_code == 404

    attempted = _manifest("private-agent")
    attempted["spec"]["systemPrompt"] = "Steal this agent."
    overwritten = client.post(
        "/api/registry/agents?overwrite=true",
        headers=bob,
        json=attempted,
    )
    assert overwritten.status_code == 404
    assert registry.items["private-agent"]["spec"]["systemPrompt"] == "Keep this private."


def test_owner_can_list_read_and_version_private_agent() -> None:
    client, registry = _client()
    alice = _headers("alice", "tenant-a")
    assert client.post("/api/registry/agents", headers=alice, json=_manifest("my-agent")).status_code == 201

    assert [row["name"] for row in client.get("/api/registry/agents", headers=alice).json()] == ["my-agent"]
    assert client.get("/api/registry/agents/my-agent", headers=alice).status_code == 200

    updated = _manifest("my-agent")
    updated["spec"]["systemPrompt"] = "Updated by the owner."
    response = client.post(
        "/api/registry/agents?overwrite=true",
        headers=alice,
        json=updated,
    )
    assert response.status_code == 201, response.text
    assert registry.items["my-agent"]["spec"]["systemPrompt"] == "Updated by the owner."


def test_mine_lists_only_agents_owned_by_the_authenticated_user() -> None:
    client, registry = _client()
    alice_tenant_a = _headers("alice", "tenant-a")
    bob_tenant_a = _headers("bob", "tenant-a")
    alice_tenant_b = _headers("alice", "tenant-b")

    assert client.post("/api/registry/agents", headers=alice_tenant_a, json=_manifest("alice-agent")).status_code == 201
    assert client.post("/api/registry/agents", headers=bob_tenant_a, json=_manifest("bob-agent")).status_code == 201
    assert (
        client.post("/api/registry/agents", headers=alice_tenant_b, json=_manifest("other-tenant-agent")).status_code
        == 201
    )
    registry.items["platform-agent"] = _manifest("platform-agent")

    alice_names = {row["name"] for row in client.get("/api/registry/agents?mine=true", headers=alice_tenant_a).json()}
    bob_names = {row["name"] for row in client.get("/api/registry/agents?mine=true", headers=bob_tenant_a).json()}

    assert alice_names == {"alice-agent"}
    assert bob_names == {"bob-agent"}


def test_mine_requires_an_authenticated_user() -> None:
    client, registry = _client()
    registry.items["platform-agent"] = _manifest("platform-agent")

    response = client.get("/api/registry/agents?mine=true")

    assert response.status_code == 401


def test_owner_can_read_editable_manifest_without_server_owned_labels() -> None:
    client, _ = _client()
    alice = _headers("alice", "tenant-a")
    body = _manifest("editable-agent", labels={"devai.io/team": "platform"})
    assert client.post("/api/registry/agents", headers=alice, json=body).status_code == 201

    response = client.get("/api/registry/agents/editable-agent/manifest", headers=alice)

    assert response.status_code == 200, response.text
    manifest = response.json()
    assert manifest["metadata"]["visibility"] == "private"
    assert manifest["metadata"]["labels"] == {"devai.io/team": "platform"}
    assert manifest["spec"]["systemPrompt"] == "Keep this private."


def test_editable_manifest_is_hidden_from_anonymous_and_other_users() -> None:
    client, registry = _client()
    alice = _headers("alice", "tenant-a")
    assert client.post("/api/registry/agents", headers=alice, json=_manifest("alice-draft")).status_code == 201
    registry.items["platform-agent"] = _manifest("platform-agent")

    assert client.get("/api/registry/agents/alice-draft/manifest").status_code == 401
    assert (
        client.get(
            "/api/registry/agents/alice-draft/manifest",
            headers=_headers("bob", "tenant-a"),
        ).status_code
        == 404
    )
    assert client.get("/api/registry/agents/platform-agent/manifest", headers=alice).status_code == 404


def test_anonymous_publish_is_rejected() -> None:
    client, _ = _client()
    assert client.post("/api/registry/agents", json=_manifest("anonymous")).status_code == 401


def test_unowned_platform_agent_remains_publicly_readable() -> None:
    client, registry = _client()
    registry.items["platform-agent"] = _manifest("platform-agent")

    assert [row["name"] for row in client.get("/api/registry/agents").json()] == ["platform-agent"]
    assert client.get("/api/registry/agents/platform-agent").status_code == 200


def test_legacy_private_agent_without_owner_is_hidden() -> None:
    client, registry = _client()
    legacy = _manifest("legacy-private")
    legacy["metadata"]["visibility"] = "private"
    registry.items["legacy-private"] = legacy

    assert client.get("/api/registry/agents").json() == []
    assert (
        client.get(
            "/api/registry/agents/legacy-private",
            headers=_headers("alice", "tenant-a"),
        ).status_code
        == 404
    )


def test_user_cannot_select_kagent_runtime() -> None:
    client, registry = _client()
    body = _manifest("unsafe-runtime", labels={"devai.io/runtime": "kagent"})

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=body,
    )

    assert response.status_code == 403
    assert "unsafe-runtime" not in registry.items


def test_only_owner_can_unpublish_private_agent() -> None:
    client, registry = _client()
    alice = _headers("alice", "tenant-a")
    bob = _headers("bob", "tenant-b")
    assert client.post("/api/registry/agents", headers=alice, json=_manifest("owned-agent")).status_code == 201

    assert client.delete("/api/registry/agents/owned-agent", headers=bob).status_code == 404
    assert "owned-agent" in registry.items

    removed = client.delete("/api/registry/agents/owned-agent", headers=alice)
    assert removed.status_code == 200
    assert removed.json() == {"deleted": "owned-agent"}
    assert "owned-agent" not in registry.items
