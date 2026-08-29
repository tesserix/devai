from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from devai.identity import Principal
from devai.orchestration.agent_lifecycle import AgentLifecycleWorkflow


@pytest.mark.asyncio
async def test_time_skipping_workflow_retries_a_transient_activity_without_duplicate_state() -> None:
    attempts = 0
    transitions: list[dict[str, Any]] = []

    @activity.defn(name="agent_import_verify")
    async def verify_import(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("Registry temporarily unavailable")
        return {"id": "import-1", "registry_ref": payload["registry_ref"]}

    @activity.defn(name="agent_lifecycle_transition")
    async def record_transition(payload: dict[str, Any]) -> dict[str, Any]:
        transitions.append(payload)
        return payload

    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue="agent-lifecycle-time-skipping",
            workflows=[AgentLifecycleWorkflow],
            activities=[verify_import, record_transition],
        ),
    ):
        result = await environment.client.execute_workflow(
            AgentLifecycleWorkflow.run,
            {
                "workflow_id": "agent-import:acme:support-lab:time-skipping",
                "operation": "import",
                "request": {
                    "principal": principal.to_dict(),
                    "registry_ref": "registry://acme/agents/acme/support@1.4.0",
                },
            },
            id="agent-import:acme:support-lab:time-skipping",
            task_queue="agent-lifecycle-time-skipping",
        )

    assert result["id"] == "import-1"
    assert attempts == 2
    assert [(event["sequence"], event["state"]) for event in transitions] == [
        (1, "running"),
        (2, "running"),
        (3, "succeeded"),
    ]


@pytest.mark.asyncio
async def test_queued_workflow_resumes_when_a_worker_restarts_after_an_outage() -> None:
    transitions: list[dict[str, Any]] = []

    @activity.defn(name="agent_promote")
    async def promote(payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "published", "agent": payload["agent"]}

    @activity.defn(name="agent_lifecycle_transition")
    async def record_transition(payload: dict[str, Any]) -> dict[str, Any]:
        transitions.append(payload)
        return payload

    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    task_queue = "agent-lifecycle-worker-restart"
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        handle = await environment.client.start_workflow(
            AgentLifecycleWorkflow.run,
            {
                "workflow_id": "agent-promote:acme:agent-lab:worker-restart",
                "operation": "promote",
                "request": {"principal": principal.to_dict(), "agent": "support"},
                "requires_approval": False,
            },
            id="agent-promote:acme:agent-lab:worker-restart",
            task_queue=task_queue,
        )
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[AgentLifecycleWorkflow],
            activities=[promote, record_transition],
        ):
            result = await handle.result()

    assert result == {"status": "published", "agent": "support"}
    assert transitions[-1]["state"] == "succeeded"
