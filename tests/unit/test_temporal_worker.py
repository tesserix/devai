from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from temporalio import activity, workflow
from temporalio.common import VersioningBehavior
from temporalio.exceptions import ApplicationError

from devai.blueprint.registry import StageRegistryError
from devai.config import Settings
from devai.orchestration import activities
from devai.orchestration.activities import run_stage_activity
from devai.orchestration.context import WorkerContext, set_worker_context
from devai.orchestration.worker import _build_state_manager, _worker_options
from devai.orchestration.workflows import BlueprintWorkflow
from devai.pipeline.types import StageResult, TaskState


class _FakeRedis:
    def __init__(self) -> None:
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        return True


class _FakeStateManager:
    instance: _FakeStateManager | None = None

    def __init__(self, *_args, **_kwargs) -> None:
        self.redis = _FakeRedis()
        type(self).instance = self


@pytest.mark.asyncio
async def test_state_manager_is_connected_before_worker_accepts_work(monkeypatch):
    from devai.core import state

    monkeypatch.setattr(state, "StateManager", _FakeStateManager)

    manager = await _build_state_manager(Settings())

    assert manager is _FakeStateManager.instance
    assert manager.redis.pings == 1


@pytest.mark.asyncio
async def test_required_state_manager_failure_aborts_worker_startup(monkeypatch):
    from devai.core import state

    class BrokenStateManager:
        def __init__(self, *_args, **_kwargs) -> None:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(state, "StateManager", BrokenStateManager)

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await _build_state_manager(Settings(temporal_worker_dependencies_required=True))


def test_versioned_worker_has_deployment_identity_and_graceful_shutdown():
    options = _worker_options(
        Settings(
            temporal_worker_versioning_enabled=True,
            temporal_worker_deployment_name="devai",
            temporal_worker_build_id="main-abc1234",
            temporal_worker_graceful_shutdown_seconds=75,
            temporal_max_concurrent_activities=24,
        )
    )

    deployment = options["deployment_config"]
    assert deployment.version.deployment_name == "devai"
    assert deployment.version.build_id == "main-abc1234"
    assert deployment.use_worker_versioning is True
    assert deployment.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE
    assert options["graceful_shutdown_timeout"].total_seconds() == 75
    assert options["max_concurrent_activities"] == 24


def test_versioned_worker_rejects_missing_build_id():
    with pytest.raises(ValueError, match="worker build ID is required"):
        _worker_options(Settings(temporal_worker_versioning_enabled=True))


class _SlowStage:
    async def execute(self, _task) -> StageResult:
        await asyncio.sleep(0.02)
        return StageResult(message="done")


class _Registry:
    def resolve(self, *_args, **_kwargs):
        return _SlowStage()


@pytest.mark.asyncio
async def test_activity_heartbeats_while_stage_is_running(monkeypatch):
    heartbeats: list[object] = []
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(activity, "heartbeat", lambda details: heartbeats.append(details))
    set_worker_context(WorkerContext(registry=_Registry(), deps=SimpleNamespace()))

    await run_stage_activity("echo", "slow-stage", {}, {"id": "task-1"})

    assert heartbeats
    assert all(item == {"stage": "slow-stage"} for item in heartbeats)


class _ApprovalStage:
    async def execute(self, task) -> StageResult:
        task.agent_context["dynamic_gates"] = [{"gate": "plan-approval"}]
        task.transition(TaskState.AWAITING_APPROVAL)
        await asyncio.sleep(0.02)
        return StageResult(message="approved")


class _ApprovalRegistry:
    def resolve(self, *_args, **_kwargs):
        return _ApprovalStage()


class _SnapshotStateManager:
    def __init__(self) -> None:
        self.snapshots: list[dict] = []

    async def persist_task(self, task: dict) -> None:
        self.snapshots.append(copy.deepcopy(task))


@pytest.mark.asyncio
async def test_activity_persists_live_stage_and_approval_progress(monkeypatch):
    manager = _SnapshotStateManager()
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(activity, "heartbeat", lambda _details: None)
    set_worker_context(
        WorkerContext(
            registry=_ApprovalRegistry(),
            deps=SimpleNamespace(state_manager=manager),
        )
    )

    result = await run_stage_activity("plan_approval", "plan-approval", {}, {"id": "task-1"})

    assert result["message"] == "approved"
    assert manager.snapshots[0]["state"] == "running"
    assert manager.snapshots[0]["current_stage"] == "plan-approval"
    assert any(
        snapshot["state"] == "awaiting_approval"
        and snapshot["agent_context"]["dynamic_gates"] == [{"gate": "plan-approval"}]
        for snapshot in manager.snapshots
    )
    final = manager.snapshots[-1]
    assert final["current_stage"] == ""
    assert "plan-approval" in final["stages_completed"]
    assert [event["phase"] for event in final["stage_events"]] == ["started", "completed"]


class _BrokenRegistry:
    def resolve(self, *_args, **_kwargs):
        raise StageRegistryError("unknown stage")


@pytest.mark.asyncio
async def test_invalid_stage_configuration_is_not_retried():
    set_worker_context(WorkerContext(registry=_BrokenRegistry(), deps=SimpleNamespace()))

    with pytest.raises(ApplicationError) as error:
        await run_stage_activity("missing", "bad-stage", {}, {"id": "task-1"})

    assert error.value.non_retryable is True
    assert error.value.type == "StageConfigurationError"


@pytest.mark.asyncio
async def test_workflow_requires_activity_heartbeats(monkeypatch):
    captured = {}

    async def execute_activity(*_args, **kwargs):
        captured.update(kwargs)
        return {"message": "done"}

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    spec = SimpleNamespace(
        stage="echo",
        name="echo",
        config={},
        timeout_seconds=90,
    )

    await BlueprintWorkflow()._run_stage(spec, {"id": "task-1"})

    assert captured["heartbeat_timeout"].total_seconds() == 30
