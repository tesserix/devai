from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any

import pytest

from devai.identity import Principal
from devai.orchestration.agent_lifecycle import AgentLifecycleWorkflow


@pytest.mark.asyncio
async def test_import_workflow_verifies_once_and_exposes_terminal_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        calls.append((activity_name, args[0]))
        return {"id": "import-1", "state": "ready"}

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))

    durable = AgentLifecycleWorkflow()
    result = await durable.run({"operation": "import", "request": {"registry_ref": "registry://exact"}})

    assert result == {"id": "import-1", "state": "ready"}
    assert calls == [("agent_import_verify", {"registry_ref": "registry://exact"})]
    assert durable.status() == {
        "operation": "import",
        "state": "succeeded",
        "step": "complete",
        "completed": ["verify_import"],
        "error": "",
        "approval": "not_required",
    }


@pytest.mark.asyncio
async def test_sandbox_provision_and_comparison_are_first_class_workflow_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        calls.append((activity_name, args[0]))
        if activity_name == "agent_sandbox_provision":
            return {"id": "sandbox-1", "status": "ready"}
        return {"id": "comparison-1"}

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))

    provision = AgentLifecycleWorkflow()
    provisioned = await provision.run({"operation": "provision", "request": {"sandbox": {"agent": {}}}})
    comparison = AgentLifecycleWorkflow()
    compared = await comparison.run(
        {"operation": "compare", "request": {"baseline_run_id": "eval-1", "candidate_run_id": "eval-2"}}
    )

    assert provisioned["id"] == "sandbox-1"
    assert compared["id"] == "comparison-1"
    assert calls == [
        ("agent_sandbox_provision", {"sandbox": {"agent": {}}}),
        ("agent_evaluation_compare", {"baseline_run_id": "eval-1", "candidate_run_id": "eval-2"}),
    ]


@pytest.mark.asyncio
async def test_workflow_appends_queryable_step_and_terminal_transitions_to_the_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[dict[str, Any]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        if activity_name == "agent_lifecycle_transition":
            transitions.append(args[0])
            return args[0]
        return {"id": "import-1", "state": "ready"}

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")

    result = await AgentLifecycleWorkflow().run(
        {
            "workflow_id": "agent-import:acme:support-lab:digest",
            "operation": "import",
            "request": {"principal": principal.to_dict(), "registry_ref": "registry://exact"},
        }
    )

    assert result["id"] == "import-1"
    assert [(item["sequence"], item["state"], item["step"]) for item in transitions] == [
        (1, "running", "queued"),
        (2, "running", "verify_import"),
        (3, "succeeded", "complete"),
    ]
    assert all(item["principal"] == principal.to_dict() for item in transitions)


@pytest.mark.asyncio
async def test_evaluation_workflow_compensates_a_provisioned_sandbox_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        calls.append(activity_name)
        if activity_name == "agent_sandbox_provision":
            return {"id": "sandbox-1", "status": "ready"}
        if activity_name == "agent_evaluation_run":
            raise RuntimeError("judge unavailable")
        return {"destroyed": args[0]["sandbox_id"]}

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))

    durable = AgentLifecycleWorkflow()
    with pytest.raises(RuntimeError, match="judge unavailable"):
        await durable.run(
            {
                "operation": "evaluate",
                "request": {"sandbox": {"model": {"provider": "portable", "model": "external"}}},
                "cleanup": True,
            }
        )

    assert calls == ["agent_sandbox_provision", "agent_evaluation_run", "agent_sandbox_cleanup"]
    assert durable.status()["state"] == "failed"
    assert durable.status()["completed"] == ["provision_sandbox", "cleanup_sandbox"]


@pytest.mark.asyncio
async def test_cancelled_workflow_records_a_queryable_terminal_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[dict[str, Any]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        if activity_name == "agent_lifecycle_transition":
            transitions.append(args[0])
            return args[0]
        raise asyncio.CancelledError

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    durable = AgentLifecycleWorkflow()

    with pytest.raises(asyncio.CancelledError):
        await durable.run(
            {
                "workflow_id": "agent-import:acme:support-lab:digest",
                "operation": "import",
                "request": {"principal": principal.to_dict()},
            }
        )

    assert durable.status()["state"] == "cancelled"
    assert (transitions[-1]["state"], transitions[-1]["step"]) == ("cancelled", "verify_import")


@pytest.mark.asyncio
async def test_timed_out_workflow_records_the_activity_step_and_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[dict[str, Any]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        if activity_name == "agent_lifecycle_transition":
            transitions.append(args[0])
            return args[0]
        raise TimeoutError("activity exceeded its deadline")

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    durable = AgentLifecycleWorkflow()

    with pytest.raises(TimeoutError, match="deadline"):
        await durable.run(
            {
                "workflow_id": "agent-eval:acme:agent-lab:digest",
                "operation": "evaluate",
                "request": {"principal": principal.to_dict(), "sandbox_id": "sandbox-1"},
            }
        )

    assert durable.status()["state"] == "timed_out"
    assert (transitions[-1]["state"], transitions[-1]["step"]) == ("timed_out", "run_evaluation")


@pytest.mark.asyncio
async def test_cleanup_failure_after_a_successful_evaluation_is_stuck_not_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[dict[str, Any]] = []

    async def execute(activity_name: str, *, args: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        if activity_name == "agent_lifecycle_transition":
            transitions.append(args[0])
            return args[0]
        if activity_name == "agent_sandbox_provision":
            return {"id": "sandbox-1", "status": "ready"}
        if activity_name == "agent_evaluation_run":
            return {"id": "eval-1", "passed": 1}
        raise RuntimeError("sandbox teardown unavailable")

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")
    durable = AgentLifecycleWorkflow()

    with pytest.raises(RuntimeError, match="teardown unavailable"):
        await durable.run(
            {
                "workflow_id": "agent-eval:acme:agent-lab:digest",
                "operation": "evaluate",
                "request": {"principal": principal.to_dict(), "cases": [{"name": "one", "input": "hello"}]},
                "cleanup": True,
            }
        )

    assert durable.status()["state"] == "stuck"
    assert transitions[-1]["state"] == "stuck"
    assert transitions[-1]["step"] == "cleanup_sandbox"


@pytest.mark.asyncio
async def test_promotion_workflow_requires_and_records_an_explicit_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def execute(activity_name: str, **_: Any) -> dict[str, Any]:
        calls.append(activity_name)
        return {"status": "published"}

    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.execute_activity", execute)
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.patched", lambda _: nullcontext(True))
    monkeypatch.setattr("devai.orchestration.agent_lifecycle.workflow.wait_condition", lambda _: _done())

    durable = AgentLifecycleWorkflow()
    durable.approve("release-manager")
    result = await durable.run({"operation": "promote", "request": {"agent": "support"}, "requires_approval": True})

    assert result == {"status": "published"}
    assert calls == ["agent_promote"]
    assert durable.status()["approval"] == "approved:release-manager"


async def _done() -> None:
    return None
