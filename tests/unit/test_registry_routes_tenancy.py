"""Object-level tenancy guarantees for the registry proxy."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.config import settings
from devai.evaluations.gates import AgentPublishGate
from devai.evaluations.models import ArtifactVersionRef
from devai.registry.client import Agent, Prompt
from devai.registry.routes import router

if TYPE_CHECKING:
    import pytest


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
            "limits": {"maxTurns": 20, "timeoutSeconds": 900},
            "riskLevel": "medium",
        },
    }


def _headers(user: str, tenant: str, *, roles: list[str] | None = None) -> dict[str, str]:
    headers = {
        "x-forwarded-user": f"{user}@example.com",
        "x-forwarded-uid": user,
        "x-forwarded-tenant": tenant,
    }
    if roles:
        headers["x-forwarded-roles"] = ",".join(roles)
    return headers


def _prompt_manifest(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Prompt",
        "metadata": {"name": name, "visibility": "public", "labels": {}},
        "spec": {"title": name, "systemPrompt": "Use the referenced instructions."},
    }


class _Registry:
    def __init__(self) -> None:
        self._namespace = "devai"
        self.items: dict[str, dict[str, Any]] = {}
        self.prompts: dict[str, dict[str, Any]] = {}

    def list_agents(self) -> list[Agent]:
        return [self._agent(body) for body in self.items.values()]

    def get_agent(self, name: str) -> Agent | None:
        body = self.items.get(name)
        return self._agent(body) if body else None

    def get_prompt(self, name: str) -> Prompt | None:
        body = self.prompts.get(name)
        if body is None:
            return None
        metadata = body["metadata"]
        spec = body["spec"]
        raw = {
            **spec,
            "name": metadata["name"],
            "labels": dict(metadata.get("labels") or {}),
            "visibility": metadata.get("visibility", ""),
        }
        return Prompt(
            name=metadata["name"],
            version=str(metadata.get("tag", "")),
            content=str(spec.get("systemPrompt", "")),
            raw=raw,
        )

    def get_artifact_envelope(self, plural: str, name: str) -> dict[str, Any] | None:
        collection = self.items if plural == "agents" else self.prompts
        body = collection.get(name)
        return deepcopy(body) if body else None

    def publish_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(body)
        name = copied["metadata"]["name"]
        self.items[name] = copied
        return {"name": name}

    def publish_prompt(self, body: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(body)
        name = copied["metadata"]["name"]
        self.prompts[name] = copied
        return {"name": name}

    def delete(self, plural: str, name: str) -> None:
        collection = self.items if plural == "agents" else self.prompts
        collection.pop(name)

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


def _client(*, gate_service: Any = None) -> tuple[TestClient, _Registry]:
    app = FastAPI()
    registry = _Registry()
    app.state.registry_client = registry
    app.state.agent_gate_service = gate_service
    app.state.config = SimpleNamespace(
        auth_bff_shared_secret="",
        trust_forwarded_without_secret=True,
    )
    app.include_router(router)
    return TestClient(app), registry


class _GateService:
    def __init__(self, gate: AgentPublishGate) -> None:
        self.gate = gate
        self.calls: list[dict[str, Any]] = []

    async def evaluate(
        self,
        principal: Any,
        manifest: dict[str, Any],
        candidate_run_id: str,
        *,
        baseline_run_id: str | None = None,
    ) -> AgentPublishGate:
        self.calls.append(
            {
                "principal": principal,
                "manifest": deepcopy(manifest),
                "candidate_run_id": candidate_run_id,
                "baseline_run_id": baseline_run_id,
            }
        )
        return self.gate

    async def override(
        self,
        principal: Any,
        gate: AgentPublishGate,
        *,
        reason: str,
    ) -> AgentPublishGate:
        if "admin" not in principal.roles:
            raise PermissionError("admin role required for an evaluation gate override")
        return gate.model_copy(
            update={
                "status": "overridden",
                "approver": principal.user_scope_id,
                "override_reason": reason,
            }
        )


def _gate(status: str, *, failing_cases: list[str] | None = None) -> AgentPublishGate:
    return AgentPublishGate(
        agent_name="gated-agent",
        status=status,
        suite=ArtifactVersionRef(name="golden", version="1"),
        candidate_run_id="eval-candidate",
        baseline_run_id="eval-baseline" if status == "blocked" else None,
        comparison_id="cmp-1" if status == "blocked" else None,
        failing_cases=failing_cases or [],
    )


def _gated_manifest() -> dict[str, Any]:
    body = _manifest("gated-agent")
    body["metadata"]["annotations"] = {
        "devai.tesserix.app/eval-run-id": "eval-candidate",
        "devai.tesserix.app/eval-approver": "attacker-chosen",
        "devai.tesserix.app/eval-gate": "passed",
    }
    body["spec"]["evalSuite"] = {"ref": "golden", "version": "1"}
    return body


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


def test_agent_publish_rejects_unknown_runtime_target() -> None:
    client, registry = _client()
    body = _manifest("unknown-runtime", labels={"devai.io/runtime": "other"})

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=body,
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "invalid_agent_runtime",
            "message": "metadata.labels.devai.io/runtime must be absent or kagent",
        }
    }
    assert "unknown-runtime" not in registry.items


def test_agent_publish_requires_an_effective_system_prompt() -> None:
    client, registry = _client()
    body = _manifest("missing-prompt")
    body["spec"]["systemPrompt"] = ""

    response = client.post("/api/registry/agents", headers=_headers("alice", "tenant-a"), json=body)

    assert response.status_code == 400
    assert "systemPrompt" in response.text or "promptRef" in response.text
    assert "missing-prompt" not in registry.items


def test_agent_publish_rejects_invalid_model_limits_and_risk() -> None:
    client, registry = _client()
    body = _manifest("invalid-contract")
    body["spec"]["model"]["provider"] = "made-up"
    body["spec"]["model"]["temperature"] = "0.3"
    body["spec"]["limits"] = {"maxTurns": 0, "timeoutSeconds": "900"}
    body["spec"]["riskLevel"] = "extreme"

    response = client.post("/api/registry/agents", headers=_headers("alice", "tenant-a"), json=body)

    assert response.status_code == 400
    assert "spec.model.provider" in response.text
    assert "spec.model.temperature" in response.text
    assert "spec.limits.maxTurns" in response.text
    assert "spec.limits.timeoutSeconds" in response.text
    assert "spec.riskLevel" in response.text
    assert "invalid-contract" not in registry.items


def test_agent_publish_accepts_runtime_provider_aliases() -> None:
    client, registry = _client()
    alice = _headers("alice", "tenant-a")

    for provider in ("claude", "gemini"):
        body = _manifest(f"{provider}-agent")
        body["spec"]["model"]["provider"] = provider
        response = client.post("/api/registry/agents", headers=alice, json=body)
        assert response.status_code == 201, response.text
        assert f"{provider}-agent" in registry.items


def test_agent_prompt_reference_must_be_visible_to_the_publisher() -> None:
    client, registry = _client()
    alice = _headers("alice", "tenant-a")
    bob = _headers("bob", "tenant-b")
    assert client.post("/api/registry/prompts", headers=bob, json=_prompt_manifest("bob-private")).status_code == 201

    body = _manifest("alice-agent")
    body["spec"]["systemPrompt"] = ""
    body["spec"]["promptRef"] = "bob-private"
    hidden = client.post("/api/registry/agents", headers=alice, json=body)
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "prompt not found: bob-private"}
    assert "alice-agent" not in registry.items

    assert (
        client.post("/api/registry/prompts", headers=alice, json=_prompt_manifest("alice-private")).status_code == 201
    )
    body["spec"]["promptRef"] = "alice-private"
    published = client.post("/api/registry/agents", headers=alice, json=body)
    assert published.status_code == 201
    assert registry.items["alice-agent"]["spec"]["systemPrompt"] == "Use the referenced instructions."
    editable = client.get("/api/registry/agents/alice-agent/manifest", headers=alice)
    assert editable.status_code == 200
    assert editable.json()["spec"]["promptRef"] == "alice-private"
    assert editable.json()["spec"]["systemPrompt"] == ""

    registry.prompts["platform-prompt"] = _prompt_manifest("platform-prompt")
    platform_agent = _manifest("platform-prompt-agent")
    platform_agent["spec"]["systemPrompt"] = ""
    platform_agent["spec"]["promptRef"] = "platform-prompt"
    assert client.post("/api/registry/agents", headers=alice, json=platform_agent).status_code == 201


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


def test_declared_eval_suite_fails_closed_when_gate_service_is_unavailable() -> None:
    client, registry = _client()

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=_gated_manifest(),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "agent evaluation gate unavailable"}
    assert "gated-agent" not in registry.items


def test_declared_eval_suite_blocks_publish_with_exact_failing_cases() -> None:
    gate_service = _GateService(_gate("blocked", failing_cases=["refund-policy"]))
    client, registry = _client(gate_service=gate_service)

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=_gated_manifest(),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "agent_evaluation_gate_blocked"
    assert response.json()["detail"]["gate"]["failing_cases"] == ["refund-policy"]
    assert "gated-agent" not in registry.items


def test_gate_override_requires_admin_and_is_server_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_emails", "admin@example.com")
    gate_service = _GateService(_gate("blocked", failing_cases=["refund-policy"]))
    client, registry = _client(gate_service=gate_service)
    override_headers = {
        "x-devai-eval-gate-override": "true",
        "x-devai-eval-gate-override-reason": "Judge outage approved by release lead",
    }

    denied = client.post(
        "/api/registry/agents",
        headers={**_headers("alice", "tenant-a"), **override_headers},
        json=_gated_manifest(),
    )
    approved = client.post(
        "/api/registry/agents",
        headers={**_headers("admin", "tenant-a", roles=["admin"]), **override_headers},
        json=_gated_manifest(),
    )

    assert denied.status_code == 403
    assert approved.status_code == 201, approved.text
    assert approved.json()["gate"]["status"] == "overridden"
    metadata = registry.items["gated-agent"]["metadata"]
    assert metadata["labels"]["devai.tesserix.app/eval-gate"] == "overridden"
    assert metadata["annotations"]["devai.tesserix.app/eval-approver"] == "tenant-a:admin"
    assert metadata["annotations"]["devai.tesserix.app/eval-override-reason"] == "Judge outage approved by release lead"


def test_passed_gate_is_server_stamped_and_returned_with_publish_result() -> None:
    gate_service = _GateService(_gate("passed"))
    client, registry = _client(gate_service=gate_service)

    response = client.post(
        "/api/registry/agents",
        headers=_headers("alice", "tenant-a"),
        json=_gated_manifest(),
    )

    assert response.status_code == 201, response.text
    assert response.json()["gate"]["status"] == "passed"
    metadata = registry.items["gated-agent"]["metadata"]
    assert metadata["labels"]["devai.tesserix.app/eval-gate"] == "passed"
    assert metadata["labels"]["devai.tesserix.app/lifecycle"] == "published"
    assert metadata["annotations"]["devai.tesserix.app/eval-run-id"] == "eval-candidate"
    assert "devai.tesserix.app/eval-approver" not in metadata["annotations"]
    assert gate_service.calls[0]["principal"].user_scope_id == "tenant-a:alice"


def test_overwrite_compares_with_server_stamped_published_baseline() -> None:
    gate_service = _GateService(_gate("passed"))
    client, registry = _client(gate_service=gate_service)
    alice = _headers("alice", "tenant-a")
    first = _gated_manifest()
    assert client.post("/api/registry/agents", headers=alice, json=first).status_code == 201
    registry.items["gated-agent"]["metadata"]["annotations"]["devai.tesserix.app/eval-run-id"] = "eval-baseline"

    response = client.post(
        "/api/registry/agents?overwrite=true",
        headers=alice,
        json=_gated_manifest(),
    )

    assert response.status_code == 201, response.text
    assert gate_service.calls[-1]["baseline_run_id"] == "eval-baseline"


def test_gated_overwrite_fails_closed_without_a_published_baseline_run() -> None:
    gate_service = _GateService(_gate("passed"))
    client, registry = _client(gate_service=gate_service)
    alice = _headers("alice", "tenant-a")
    first = _gated_manifest()
    first["spec"].pop("evalSuite")
    first["metadata"].pop("annotations")
    assert client.post("/api/registry/agents", headers=alice, json=first).status_code == 201
    registry.items["gated-agent"]["spec"]["evalSuite"] = {"ref": "golden", "version": "1"}

    response = client.post(
        "/api/registry/agents?overwrite=true",
        headers=alice,
        json=_gated_manifest(),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "agent_evaluation_baseline_required"
    assert len(gate_service.calls) == 0
