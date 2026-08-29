from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.registry.client import Agent, ResolvedAgent, UnresolvedRef
from devai.registry.import_routes import router
from devai.registry.imports import AgentImportService

_SIGNING_KEY = Ed25519PrivateKey.generate()


def _headers(user: str, tenant: str) -> dict[str, str]:
    return {
        "x-forwarded-user": f"{user}@example.com",
        "x-forwarded-uid": user,
        "x-forwarded-tenant": tenant,
        "idempotency-key": "publish-run-42",
    }


class _Registry:
    def __init__(
        self,
        *,
        runtime_type: str = "remote",
        unresolved: bool = False,
        tampered_signature: bool = False,
    ) -> None:
        self.runtime_type = runtime_type
        self.unresolved = unresolved
        self.tampered_signature = tampered_signature

    def get_signing_key(self) -> dict[str, Any]:
        public_key = _SIGNING_KEY.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {
            "enabled": True,
            "algorithm": "ed25519",
            "keyId": "registry-key-1",
            "publicKey": base64.b64encode(public_key).decode(),
            "signs": "digest",
        }

    def resolve_agent(self, name: str, *, namespace: str = "", tag: str = "") -> ResolvedAgent:
        assert (name, namespace, tag) == ("support", "acme", "1.4.0")
        runtime = (
            {
                "type": "remote",
                "protocol": "a2a",
                "url": "https://agents.acme.example/support",
                "auth": {
                    "type": "bearer",
                    "credentialRef": "openbao://agents/support-token",
                },
            }
            if self.runtime_type == "remote"
            else {
                "type": "container",
                "protocol": "a2a",
                "image": "ghcr.io/acme/support@sha256:" + "c" * 64,
                "port": 9090,
                "path": "/a2a/v1",
                "healthPath": "/readyz",
            }
        )
        agent_spec = {
            "definitionVersion": "v1",
            "framework": "langgraph",
            "runtime": runtime,
            "skills": [{"ref": "triage", "version": "1.0.0"}],
        }
        digest = "sha256:" + "a" * 64
        signing_key = Ed25519PrivateKey.generate() if self.tampered_signature else _SIGNING_KEY
        envelope = {
            "apiVersion": "registry.agentic.dev/v1alpha1",
            "kind": "Agent",
            "metadata": {
                "name": "support",
                "namespace": "acme",
                "tenantId": "acme",
                "tag": "1.4.0",
                "digest": digest,
                "signature": base64.b64encode(signing_key.sign(digest.encode())).decode(),
                "signedBy": "registry-key-1",
            },
            "spec": agent_spec,
        }
        dependency = {
            "apiVersion": "registry.agentic.dev/v1alpha1",
            "kind": "Skill",
            "metadata": {
                "name": "triage",
                "namespace": "acme",
                "tag": "1.0.0",
                "digest": "sha256:" + "b" * 64,
            },
            "spec": {"description": "Triage a support request"},
        }
        return ResolvedAgent(
            agent=Agent(
                name="support",
                description="Support agent",
                version="1.4.0",
                framework="langgraph",
                raw=agent_spec,
            ),
            resolved={"skills": [dependency]},
            unresolved=(
                [UnresolvedRef(kind="Skill", ref="triage@1.0.0", reason="not found")] if self.unresolved else []
            ),
            envelope=envelope,
        )


class _Database:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_agent_import(self, **values: Any) -> dict[str, Any]:
        existing = next(
            (
                row
                for row in self.rows.values()
                if row["owner_scope"] == values["owner_scope"]
                and row["project_id"] == values["project_id"]
                and row["idempotency_key"] == values["idempotency_key"]
            ),
            None,
        )
        if existing is not None:
            return deepcopy(existing)
        self.rows[values["id"]] = deepcopy(values)
        return deepcopy(values)

    async def get_agent_import(self, owner_scope: str, import_id: str) -> dict[str, Any] | None:
        row = self.rows.get(import_id)
        return deepcopy(row) if row and row["owner_scope"] == owner_scope else None

    async def get_agent_import_by_idempotency(
        self, owner_scope: str, project_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = next(
            (
                row
                for row in self.rows.values()
                if row["owner_scope"] == owner_scope
                and row["project_id"] == project_id
                and row["idempotency_key"] == idempotency_key
            ),
            None,
        )
        return deepcopy(row) if row else None

    async def list_agent_imports(self, owner_scope: str, project_id: str, *, limit: int) -> list[dict[str, Any]]:
        return [
            deepcopy(row)
            for row in self.rows.values()
            if row["owner_scope"] == owner_scope and row["project_id"] == project_id
        ][:limit]


def _client(registry: _Registry | None = None) -> tuple[TestClient, _Database]:
    app = FastAPI()
    database = _Database()
    app.state.agent_import_service = AgentImportService(database=database, registry=registry or _Registry())
    app.state.config = SimpleNamespace(
        auth_bff_shared_secret="",
        trust_forwarded_without_secret=True,
    )
    app.include_router(router)
    return TestClient(app), database


def test_import_pins_registry_agent_and_dependency_lock() -> None:
    client, _ = _client()

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "ready"
    assert body["conformance"]["level"] == "callable"
    assert body["agent"]["digest"] == "sha256:" + "a" * 64
    assert body["agent"]["runtime"]["auth"] == {
        "type": "bearer",
        "credentialRef": "openbao://agents/support-token",
    }
    assert body["dependency_lock"] == [
        {
            "kind": "Skill",
            "name": "triage",
            "namespace": "acme",
            "version": "1.0.0",
            "digest": "sha256:" + "b" * 64,
        }
    ]


def test_import_idempotency_replays_the_original_snapshot() -> None:
    client, database = _client()
    request = {
        "headers": _headers("alice", "acme"),
        "json": {
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    }

    first = client.post("/api/registry/imports", **request)
    second = client.post("/api/registry/imports", **request)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(database.rows) == 1


def test_import_uses_durable_orchestration_when_configured() -> None:
    client, database = _client()

    class _Orchestrator:
        request: dict[str, str] | None = None

        async def import_agent(self, principal: Any, **request: str) -> dict[str, Any]:
            self.request = {"tenant_id": principal.tenant_id, **request}
            return {"id": "workflow-import", "state": "ready"}

    orchestrator = _Orchestrator()
    client.app.state.agent_lifecycle_orchestrator = orchestrator

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    )

    assert response.status_code == 201
    assert response.json() == {"id": "workflow-import", "state": "ready"}
    assert orchestrator.request == {
        "tenant_id": "acme",
        "project_id": "support-lab",
        "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        "idempotency_key": "publish-run-42",
    }
    assert database.rows == {}


def test_import_rejects_mutable_registry_reference() -> None:
    client, _ = _client()

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@latest",
        },
    )

    assert response.status_code == 422
    assert "immutable" in response.json()["detail"]


def test_import_blocks_unresolved_dependency_graph() -> None:
    client, _ = _client(_Registry(unresolved=True))

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    )

    assert response.status_code == 422
    assert "unresolved" in response.json()["detail"]


def test_import_rejects_invalid_registry_signature() -> None:
    client, _ = _client(_Registry(tampered_signature=True))

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    )

    assert response.status_code == 422
    assert "signature" in response.json()["detail"]


def test_container_import_is_sandbox_runnable() -> None:
    client, _ = _client(_Registry(runtime_type="container"))

    response = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["conformance"]["level"] == "sandbox_runnable"
    assert response.json()["agent"]["runtime"] == {
        "type": "container",
        "protocol": "a2a",
        "image": "ghcr.io/acme/support@sha256:" + "c" * 64,
        "port": 9090,
        "path": "/a2a/v1",
        "healthPath": "/readyz",
    }


def test_foreign_tenant_cannot_read_import() -> None:
    client, _ = _client()
    created = client.post(
        "/api/registry/imports",
        headers=_headers("alice", "acme"),
        json={
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        },
    ).json()

    response = client.get(
        f"/api/registry/imports/{created['id']}",
        headers=_headers("mallory", "other-tenant"),
    )

    assert response.status_code == 404
