from __future__ import annotations

import asyncio
import copy
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio import activity, workflow
from temporalio.common import VersioningBehavior
from temporalio.exceptions import ApplicationError

from devai.blueprint.registry import StageRegistryError
from devai.config import Settings
from devai.orchestration import activities
from devai.orchestration.activities import publish_progress_activity, run_stage_activity
from devai.orchestration.context import WorkerContext, set_worker_context
from devai.orchestration.worker import _build_state_manager, _worker_options
from devai.orchestration.workflows import BlueprintWorkflow
from devai.pipeline.types import StageEvent, StageEventPhase, StageResult, TaskState


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

    inbound = {
        "id": "task-1",
        "stage_events": [
            StageEvent(
                "plan-approval",
                StageEventPhase.STARTED,
                agent="product_director",
                lane="plan",
            ).to_dict()
        ],
        "agents": {"product_director": {"status": "running", "stage": "plan-approval"}},
    }

    result = await run_stage_activity("plan_approval", "plan-approval", {}, inbound)

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
    # The workflow owns the timeline; the activity must carry it through intact
    # rather than appending a second, agent-less copy of each event.
    assert [(e["phase"], e["agent"]) for e in final["stage_events"]] == [("started", "product_director")]
    assert final["agents"]["product_director"]["status"] == "running"


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


# ── The durable path must produce the same timeline as the in-process one ──


def _two_stage_payload():
    """A two-stage blueprint payload: one agentic stage, one skipped by condition."""
    return {
        "blueprint": {
            "name": "alm-pipeline",
            "description": "",
            "stages": [
                {
                    "name": "analyze-requirements",
                    "stage": "echo",
                    "depends_on": [],
                    "config": {},
                    "type": "agentic",
                    "lane": "plan",
                    "agent": "requirements_analyst",
                },
                {
                    "name": "deploy",
                    "stage": "echo",
                    "depends_on": ["analyze-requirements"],
                    "config": {},
                    "type": "deploy",
                    "lane": "ship",
                    "agent": "release_manager",
                    "condition": "task.has_pr",
                },
            ],
            "metadata": {},
        },
        "task": {"id": "devai-timeline", "repo": "tesserix/test-repo", "blueprint": "alm-pipeline"},
    }


def _is_publish(fn) -> bool:
    return "publish_progress" in getattr(fn, "__name__", str(fn))


async def _run_workflow(monkeypatch, payload, activity_result=None):
    async def execute_activity(fn, **_kwargs):
        if _is_publish(fn):
            return None
        return activity_result if activity_result is not None else {"message": "done", "data": {}}

    async def wait_condition(predicate, *_args, **_kwargs):
        return predicate()

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(workflow, "logger", logging.getLogger("test-workflow"))
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 8, 27, tzinfo=UTC))
    return await BlueprintWorkflow().run(payload)


@pytest.mark.asyncio
async def test_workflow_records_stage_events_with_agent_attribution(monkeypatch):
    """The Temporal backend must emit StageEvents like BlueprintExecutor does.

    BlueprintWorkflow only appended to stages_completed, so a durable run
    finished with an empty stage_events list. The dashboard derives its agent
    cards from those events, so every Temporal run showed "no agents yet".
    """
    result = await _run_workflow(monkeypatch, _two_stage_payload())

    events = result["stage_events"]
    started = [e for e in events if e["stage"] == "analyze-requirements" and e["phase"] == "started"]
    completed = [e for e in events if e["stage"] == "analyze-requirements" and e["phase"] == "completed"]

    assert len(started) == 1, events
    assert len(completed) == 1, events
    assert started[0]["agent"] == "requirements_analyst"
    assert started[0]["lane"] == "plan"
    assert completed[0]["agent"] == "requirements_analyst"


@pytest.mark.asyncio
async def test_workflow_records_skipped_stages_on_the_timeline(monkeypatch):
    result = await _run_workflow(monkeypatch, _two_stage_payload())

    skipped = [e for e in result["stage_events"] if e["phase"] == "skipped"]

    assert [e["stage"] for e in skipped] == ["deploy"]
    assert skipped[0]["agent"] == "release_manager"


@pytest.mark.asyncio
async def test_workflow_records_failed_stage_on_the_timeline(monkeypatch):
    payload = _two_stage_payload()
    result = await _run_workflow(monkeypatch, payload, activity_result={"message": "boom", "data": {"ok": False}})

    failed = [e for e in result["stage_events"] if e["phase"] == "failed"]

    assert [e["stage"] for e in failed] == ["analyze-requirements"]
    assert failed[0]["agent"] == "requirements_analyst"
    assert "boom" in (failed[0]["error"] or "")


@pytest.mark.asyncio
async def test_workflow_populates_agent_cards_as_stages_run(monkeypatch):
    """`task.agents` is what the dashboard counts for "N/M agents".

    On the in-process path the pipeline service derives it from stage events.
    The durable path has no such hook, so the workflow must project the same
    status onto the task or a Temporal run renders "no agents yet".
    """
    result = await _run_workflow(monkeypatch, _two_stage_payload())

    agents = result["agents"]

    assert agents["requirements_analyst"]["status"] == "completed"
    assert agents["requirements_analyst"]["stage"] == "analyze-requirements"
    assert agents["release_manager"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_workflow_marks_agent_running_before_the_stage_finishes(monkeypatch):
    """The snapshot handed to the activity must already show the agent running,
    so a long stage reports progress instead of looking queued."""

    async def wait_condition(predicate, *_a, **_k):
        return predicate()

    monkeypatch.setattr(workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(workflow, "logger", logging.getLogger("test-workflow"))
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 8, 27, tzinfo=UTC))

    captured: list[dict] = []

    async def capture_activity(fn, *, args, **_kwargs):
        if _is_publish(fn):
            return None
        captured.append(args[3])
        return {"message": "done", "data": {}}

    monkeypatch.setattr(workflow, "execute_activity", capture_activity)
    await BlueprintWorkflow().run(_two_stage_payload())

    assert captured, "no activity was dispatched"
    assert captured[0]["agents"]["requirements_analyst"]["status"] == "running"


# ── Live progress: the durable path must report while it runs, not only after ─


class _RecordingStateManager:
    def __init__(self) -> None:
        self.snapshots: list[dict] = []

    async def persist_task(self, task_dict, **_kwargs) -> None:
        self.snapshots.append(task_dict)


@pytest.mark.asyncio
async def test_publish_activity_pushes_the_workflow_snapshot_verbatim():
    """The API pod is a different process from the worker, so in-process event
    callbacks never reach the dashboard. Workflow-authored events reach it only
    through this activity, and must arrive unaltered."""
    state = _RecordingStateManager()
    set_worker_context(WorkerContext(registry=_Registry(), deps=SimpleNamespace(state_manager=state)))
    snapshot = {"id": "task-1", "agents": {"a": {"status": "completed"}}, "updated_at": 42.0}

    await publish_progress_activity(snapshot)

    assert state.snapshots == [snapshot]


@pytest.mark.asyncio
async def test_publish_activity_survives_an_unavailable_state_manager():
    class _Broken:
        async def persist_task(self, *_a, **_k):
            raise ConnectionError("valkey down")

    set_worker_context(WorkerContext(registry=_Registry(), deps=SimpleNamespace(state_manager=_Broken())))

    await publish_progress_activity({"id": "task-1"})


@pytest.mark.asyncio
async def test_activity_survives_an_unavailable_state_manager(monkeypatch):
    """Progress reporting is best-effort — it must never fail the stage."""

    class _Broken:
        async def persist_task(self, *_a, **_k):
            raise ConnectionError("valkey down")

    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(activity, "heartbeat", lambda details: None)
    set_worker_context(WorkerContext(registry=_Registry(), deps=SimpleNamespace(state_manager=_Broken())))

    result = await run_stage_activity("echo", "slow-stage", {}, {"id": "task-1"})

    assert result["message"]


@pytest.mark.asyncio
async def test_workflow_publishes_progress_after_every_stage(monkeypatch):
    """A stage's terminal event must reach the dashboard immediately — the last
    stage and any gate would otherwise hold it until the whole run returns."""
    published: list[dict] = []

    async def execute_activity(fn, *, args, **_kwargs):
        if _is_publish(fn):
            published.append(args[0])
            return None
        return {"message": "done", "data": {}}

    async def wait_condition(predicate, *_a, **_k):
        return predicate()

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(workflow, "logger", logging.getLogger("test-workflow"))
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 8, 27, tzinfo=UTC))

    await BlueprintWorkflow().run(_two_stage_payload())

    assert published, "workflow never published a progress snapshot"
    last = published[-1]
    assert last["agents"]["requirements_analyst"]["status"] == "completed"


def test_worker_registers_the_progress_activity():
    """An activity the workflow calls but the worker never registered fails the
    run at dispatch time, not at import time — assert the wiring directly."""
    import inspect

    from devai.orchestration import worker as worker_module

    source = inspect.getsource(worker_module.run_worker)

    assert "publish_progress_activity" in source
