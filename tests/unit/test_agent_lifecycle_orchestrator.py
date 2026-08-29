from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from devai.identity import Principal
from devai.orchestration.agent_lifecycle_client import AgentLifecycleOrchestrator


class _Handle:
    async def result(self) -> dict[str, Any]:
        return {"id": "import-1", "state": "ready"}


class _TemporalClient:
    started: dict[str, Any] | None = None

    async def start_workflow(self, name: str, payload: dict[str, Any], **options: Any) -> _Handle:
        self.started = {"name": name, "payload": payload, **options}
        return _Handle()


@pytest.mark.asyncio
async def test_import_orchestrator_uses_a_stable_tenant_scoped_workflow_id() -> None:
    client = _TemporalClient()
    orchestrator = AgentLifecycleOrchestrator(
        SimpleNamespace(temporal_task_queue="devai"),
        client=client,
    )
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")

    result = await orchestrator.import_agent(
        principal,
        project_id="support-lab",
        registry_ref="registry://acme/agents/acme/support@1.4.0",
        idempotency_key="publisher-run-42",
    )

    assert result == {"id": "import-1", "state": "ready"}
    assert client.started is not None
    assert client.started["name"] == "AgentLifecycleWorkflow"
    assert client.started["id"].startswith("agent-import:acme:support-lab:")
    assert "publisher-run-42" not in client.started["id"]
    assert client.started["payload"] == {
        "workflow_id": client.started["id"],
        "operation": "import",
        "request": {
            "principal": principal.to_dict(),
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
            "idempotency_key": "publisher-run-42",
        },
    }


@pytest.mark.asyncio
async def test_sandbox_orchestrator_uses_the_request_key_for_one_durable_resource() -> None:
    client = _TemporalClient()
    orchestrator = AgentLifecycleOrchestrator(SimpleNamespace(temporal_task_queue="devai"), client=client)
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    spec = {
        "agent": {"name": "support", "version": "1.4.0"},
        "model": {"provider": "portable", "model": "external"},
    }

    await orchestrator.provision(principal, spec, request_id="sandbox-request-42")

    assert client.started is not None
    assert client.started["id"].startswith("agent-sandbox:acme:agent-lab:")
    assert client.started["payload"] == {
        "workflow_id": client.started["id"],
        "operation": "provision",
        "request": {
            "principal": principal.to_dict(),
            "sandbox": spec,
            "idempotency_key": "sandbox-request-42",
            "idempotency_scope": "sandbox",
        },
    }


@pytest.mark.asyncio
async def test_comparison_orchestrator_is_idempotent_for_the_supplied_request_key() -> None:
    client = _TemporalClient()
    orchestrator = AgentLifecycleOrchestrator(SimpleNamespace(temporal_task_queue="devai"), client=client)
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")

    await orchestrator.compare(
        principal,
        {"baseline_run_id": "eval-1", "candidate_run_id": "eval-2"},
        request_id="compare-request-42",
    )

    assert client.started is not None
    assert client.started["id"].startswith("agent-compare:acme:agent-lab:")
    assert client.started["payload"] == {
        "workflow_id": client.started["id"],
        "operation": "compare",
        "request": {
            "principal": principal.to_dict(),
            "baseline_run_id": "eval-1",
            "candidate_run_id": "eval-2",
            "idempotency_key": "compare-request-42",
        },
    }
