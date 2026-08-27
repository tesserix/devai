"""Contract tests for the workflow (durable orchestration) adapter family.

These prove the generic backbone runs *any* blueprint with zero per-blueprint
code, that the in-process backend is behaviour-identical to the executor, and that
provider selection degrades gracefully. The Temporal path's deterministic logic
(ordering, serde, failure policy) is exercised here without needing a cluster.
"""

from __future__ import annotations

import base64
import builtins
from pathlib import Path

import pytest

from devai.adapters.workflow import (
    InProcWorkflowAdapter,
    NoopWorkflowAdapter,
    create_workflow_adapter,
)
from devai.adapters.workflow.temporal import TemporalWorkflowAdapter, workflow_id_for_task
from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import (
    StageSpec,
    discover_blueprints,
    load_blueprint_from_string,
)
from devai.blueprint.planner import should_continue_on_failure, topological_levels
from devai.blueprint.registry import StageRegistry
from devai.config import Settings
from devai.orchestration.payload_codec import EncryptedPayloadCodec, temporal_data_converter
from devai.orchestration.serde import (
    blueprint_from_dict,
    blueprint_to_dict,
    stage_result_from_dict,
    stage_result_to_dict,
    task_from_dict,
    task_to_dict,
)
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

# ── Fakes ────────────────────────────────────────────────────────────────


class _EchoStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self._config = config

    def name(self) -> str:
        return self._config.get("__stage_name", "echo")

    async def execute(self, task: DevAITask) -> StageResult:
        return StageResult(message="ok", data={f"{self.name()}_output": {"ran": True}})


class _BoomStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self._config = config

    def name(self) -> str:
        return self._config.get("__stage_name", "boom")

    async def execute(self, task: DevAITask) -> StageResult:
        raise RuntimeError("kaboom")


def _registry() -> StageRegistry:
    reg = StageRegistry()
    reg.register("echo", lambda deps, cfg: _EchoStage(deps, cfg))
    reg.register("boom", lambda deps, cfg: _BoomStage(deps, cfg))
    return reg


def _inproc(settings: Settings | None = None) -> InProcWorkflowAdapter:
    settings = settings or Settings()
    deps = StageDeps(config=settings)
    return InProcWorkflowAdapter(BlueprintExecutor(_registry(), deps))


_LINEAR_BP = """
name: t-linear
stages:
  - name: a
    stage: echo
  - name: b
    stage: echo
    depends_on: [a]
"""

_DIAMOND_BP = """
name: t-diamond
stages:
  - name: a
    stage: echo
  - name: b
    stage: echo
    depends_on: [a]
  - name: c
    stage: echo
    depends_on: [a]
  - name: d
    stage: echo
    depends_on: [b, c]
"""


# ── planner ───────────────────────────────────────────────────────────────


def _specs(*edges: tuple[str, list[str]]) -> list[StageSpec]:
    return [StageSpec(name=n, stage="echo", depends_on=d) for n, d in edges]


def test_topo_linear():
    levels = topological_levels(_specs(("a", []), ("b", ["a"]), ("c", ["b"])))
    assert [[s.name for s in lvl] for lvl in levels] == [["a"], ["b"], ["c"]]


def test_topo_diamond():
    levels = topological_levels(_specs(("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])))
    names = [[s.name for s in lvl] for lvl in levels]
    assert names[0] == ["a"]
    assert sorted(names[1]) == ["b", "c"]
    assert names[2] == ["d"]


def test_topo_cycle_raises():
    with pytest.raises(ValueError):
        topological_levels(_specs(("a", ["b"]), ("b", ["a"])))


def test_should_continue_on_failure():
    assert should_continue_on_failure("continue") is True
    assert should_continue_on_failure("stop") is False
    assert should_continue_on_failure("rollback") is False
    assert should_continue_on_failure("") is False


# ── factory selection ──────────────────────────────────────────────────────


def test_factory_default_is_inproc():
    settings = Settings()
    deps = StageDeps(config=settings)
    ex = BlueprintExecutor(_registry(), deps)
    assert isinstance(create_workflow_adapter(settings, executor=ex), InProcWorkflowAdapter)


def test_factory_noop():
    settings = Settings(workflow_provider="noop")
    ex = BlueprintExecutor(_registry(), StageDeps(config=settings))
    assert isinstance(create_workflow_adapter(settings, executor=ex), NoopWorkflowAdapter)


def test_factory_unknown_degrades_to_inproc():
    settings = Settings(workflow_provider="banana")
    ex = BlueprintExecutor(_registry(), StageDeps(config=settings))
    assert isinstance(create_workflow_adapter(settings, executor=ex), InProcWorkflowAdapter)


def test_factory_temporal_without_sdk_degrades(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "temporalio":
            raise ImportError("simulated missing temporalio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    settings = Settings(workflow_provider="temporal")
    ex = BlueprintExecutor(_registry(), StageDeps(config=settings))
    adapter = create_workflow_adapter(settings, executor=ex)
    assert isinstance(adapter, InProcWorkflowAdapter)


def test_temporal_workflow_id_is_opaque_and_principal_scoped():
    first = DevAITask(
        id="same-task",
        principal={"tenant_id": "tenant-a", "uid": "user-1", "email": "a@example.test"},
    )
    second = DevAITask(
        id="same-task",
        principal={"tenant_id": "tenant-b", "uid": "user-1", "email": "b@example.test"},
    )

    first_id = workflow_id_for_task(first)
    second_id = workflow_id_for_task(second)

    assert first_id != second_id
    assert first_id.endswith("-same-task")
    assert "tenant-a" not in first_id
    assert "a@example.test" not in first_id


class _RecordingFallback(NoopWorkflowAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def run_blueprint(self, blueprint, task):
        self.calls += 1
        return task


@pytest.mark.asyncio
async def test_temporal_connect_failure_does_not_replay_in_fail_closed_mode(monkeypatch):
    fallback = _RecordingFallback()
    adapter = TemporalWorkflowAdapter(
        Settings(temporal_fail_closed=True),
        fallback=fallback,
    )

    async def fail_connect():
        raise ConnectionError("unavailable")

    monkeypatch.setattr(adapter, "_ensure_client", fail_connect)
    task = DevAITask(blueprint="t-linear")
    result = await adapter.run_blueprint(load_blueprint_from_string(_LINEAR_BP), task)

    assert result is task
    assert result.state == TaskState.STAGE_FAILED
    assert result.error == "durable workflow backend unavailable"
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_temporal_connect_failure_keeps_local_fallback_when_not_strict(monkeypatch):
    fallback = _RecordingFallback()
    adapter = TemporalWorkflowAdapter(Settings(), fallback=fallback)

    async def fail_connect():
        raise ConnectionError("unavailable")

    monkeypatch.setattr(adapter, "_ensure_client", fail_connect)
    task = DevAITask(blueprint="t-linear")
    result = await adapter.run_blueprint(load_blueprint_from_string(_LINEAR_BP), task)

    assert result is task
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_temporal_already_started_reuses_the_scoped_workflow():
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    task = DevAITask(
        blueprint="t-linear",
        principal={"tenant_id": "tenant-a", "uid": "user-1"},
    )
    expected_id = workflow_id_for_task(task)

    class Handle:
        async def result(self):
            return task_to_dict(task)

    class Client:
        requested_id = ""
        requested_policy = None

        async def start_workflow(self, *_args, **kwargs):
            self.requested_policy = kwargs.get("id_reuse_policy")
            raise WorkflowAlreadyStartedError(kwargs["id"], "BlueprintWorkflow")

        def get_workflow_handle(self, workflow_id):
            self.requested_id = workflow_id
            return Handle()

    client = Client()
    adapter = TemporalWorkflowAdapter(Settings(), fallback=NoopWorkflowAdapter())
    adapter._client = client

    await adapter.run_blueprint(load_blueprint_from_string(_LINEAR_BP), task)

    assert client.requested_id == expected_id
    assert client.requested_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE


@pytest.mark.asyncio
async def test_temporal_resume_allows_a_new_execution_after_business_failure():
    from temporalio.common import WorkflowIDReusePolicy

    task = DevAITask(blueprint="t-linear")
    task.agent_context["resumed_from_failure_at"] = 123.0

    class Handle:
        async def result(self):
            return task_to_dict(task)

    class Client:
        requested_policy = None

        async def start_workflow(self, *_args, **kwargs):
            self.requested_policy = kwargs.get("id_reuse_policy")
            return Handle()

    client = Client()
    adapter = TemporalWorkflowAdapter(Settings(), fallback=NoopWorkflowAdapter())
    adapter._client = client

    await adapter.run_blueprint(load_blueprint_from_string(_LINEAR_BP), task)

    assert client.requested_policy == WorkflowIDReusePolicy.ALLOW_DUPLICATE


@pytest.mark.asyncio
async def test_temporal_result_updates_the_original_queued_task():
    task = DevAITask(blueprint="t-linear")
    completed = DevAITask.from_dict(task.to_dict())
    completed.stages_completed = ["a", "b"]
    completed.transition(TaskState.COMPLETED)

    class Handle:
        async def result(self):
            return task_to_dict(completed)

    class Client:
        async def start_workflow(self, *_args, **_kwargs):
            return Handle()

    adapter = TemporalWorkflowAdapter(Settings(), fallback=NoopWorkflowAdapter())
    adapter._client = Client()

    result = await adapter.run_blueprint(load_blueprint_from_string(_LINEAR_BP), task)

    assert result is task
    assert task.state == TaskState.COMPLETED
    assert task.stages_completed == ["a", "b"]


@pytest.mark.asyncio
async def test_temporal_payload_codec_roundtrip_hides_plaintext():
    from temporalio.api.common.v1 import Payload

    key = base64.b64encode(b"k" * 32).decode()
    codec = EncryptedPayloadCodec.from_base64(key)
    original = Payload(metadata={"encoding": b"json/plain"}, data=b"secret@example.test")

    encoded = await codec.encode([original])
    assert b"secret@example.test" not in encoded[0].data
    assert encoded[0].metadata["encoding"] == b"binary/encrypted"

    decoded = await codec.decode(encoded)
    assert decoded == [original]


def test_temporal_payload_encryption_key_is_required_when_configured():
    with pytest.raises(ValueError, match="payload encryption key is required"):
        temporal_data_converter(
            Settings(temporal_payload_encryption_required=True),
        )


# ── inproc execution = executor behaviour ──────────────────────────────────


@pytest.mark.asyncio
async def test_inproc_runs_linear_blueprint():
    adapter = _inproc()
    bp = load_blueprint_from_string(_LINEAR_BP)
    out = await adapter.run_blueprint(bp, DevAITask(blueprint="t-linear"))
    assert out.state == TaskState.COMPLETED
    assert out.stages_completed == ["a", "b"]
    assert out.agent_context["a_output"] == {"ran": True}
    assert out.agent_context["b_output"] == {"ran": True}


@pytest.mark.asyncio
async def test_inproc_runs_diamond_blueprint():
    adapter = _inproc()
    bp = load_blueprint_from_string(_DIAMOND_BP)
    out = await adapter.run_blueprint(bp, DevAITask(blueprint="t-diamond"))
    assert out.state == TaskState.COMPLETED
    assert set(out.stages_completed) == {"a", "b", "c", "d"}


@pytest.mark.asyncio
async def test_inproc_stage_failure_halts():
    settings = Settings()
    deps = StageDeps(config=settings)
    adapter = InProcWorkflowAdapter(BlueprintExecutor(_registry(), deps))
    bp = load_blueprint_from_string(
        """
name: t-fail
stages:
  - name: a
    stage: boom
  - name: b
    stage: echo
    depends_on: [a]
"""
    )
    out = await adapter.run_blueprint(bp, DevAITask(blueprint="t-fail"))
    assert out.is_failed
    assert "b" not in out.stages_completed


@pytest.mark.asyncio
async def test_noop_returns_task_unchanged():
    adapter = NoopWorkflowAdapter()
    bp = load_blueprint_from_string(_LINEAR_BP)
    task = DevAITask(blueprint="t-linear")
    out = await adapter.run_blueprint(bp, task)
    assert out is task
    assert out.stages_completed == []


# ── serde round-trips (Temporal payload safety) ────────────────────────────


def test_task_roundtrip():
    t = DevAITask(intent="ship it", repo="org/repo", blueprint="alm-pipeline")
    t.state = TaskState.REVIEWING
    t.pr_number = 42
    t.agent_context = {"coder_output": {"files": 3}}
    t.stages_completed = ["a", "b"]
    out = task_from_dict(task_to_dict(t))
    assert out.intent == "ship it"
    assert out.repo == "org/repo"
    assert out.state == TaskState.REVIEWING
    assert out.pr_number == 42
    assert out.agent_context == {"coder_output": {"files": 3}}
    assert out.stages_completed == ["a", "b"]


def test_blueprint_roundtrip():
    bp = load_blueprint_from_string(_DIAMOND_BP)
    out = blueprint_from_dict(blueprint_to_dict(bp))
    assert out.name == bp.name
    assert [s.name for s in out.stages] == [s.name for s in bp.stages]
    assert [s.depends_on for s in out.stages] == [s.depends_on for s in bp.stages]


def test_stage_result_roundtrip():
    r = StageResult(next_state=TaskState.TESTING, message="done", data={"k": [1, 2]})
    out = stage_result_from_dict(stage_result_to_dict(r))
    assert out.next_state == TaskState.TESTING
    assert out.message == "done"
    assert out.data == {"k": [1, 2]}


# ── "any blueprint, simple to complex" — every shipped blueprint ────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _shipped_blueprints():
    return discover_blueprints(_REPO_ROOT / "blueprints")


def test_shipped_blueprints_exist():
    assert _shipped_blueprints(), "no blueprints found under blueprints/"


@pytest.mark.parametrize("name", sorted(_shipped_blueprints().keys()))
def test_shipped_blueprint_orders_and_serializes(name):
    """The generic backbone must order + serialize EVERY shipped blueprint
    (simple pr-review through complex alm-pipeline) with no per-blueprint code."""
    bp = _shipped_blueprints()[name]

    # 1) The DAG sorts into levels with no lost/duplicated stages (no cycles).
    levels = topological_levels(bp.stages)
    flat = [s.name for lvl in levels for s in lvl]
    assert sorted(flat) == sorted(s.name for s in bp.stages)

    # 2) The Temporal payload round-trips losslessly (workflow boundary safety).
    rt = blueprint_from_dict(blueprint_to_dict(bp))
    assert [s.name for s in rt.stages] == [s.name for s in bp.stages]
    assert [s.stage for s in rt.stages] == [s.stage for s in bp.stages]
    assert [s.depends_on for s in rt.stages] == [s.depends_on for s in bp.stages]


def test_supervisor_led_blueprint_is_durable():
    """The supervisor flow (legacy LangGraph `_node_supervisor`) is now a stage in
    a blueprint, so it runs through the generic Temporal workflow with no special
    code: the Supervisor plans (run_specialization:supervisor) after tech detection
    and before the delegated planning chain."""
    bp = _shipped_blueprints()["supervisor-alm"]
    sup = bp.stage_by_name("supervise")
    assert sup is not None, "supervisor stage missing"
    assert sup.stage == "run_specialization"
    assert sup.config.get("specialization") == "supervisor"

    order = {s.name: i for i, lvl in enumerate(topological_levels(bp.stages)) for s in lvl}
    assert order["detect-tech-stack"] < order["supervise"] < order["analyze-requirements"]


# ── run control (pause / stop) — dashboard buttons, durable path ───────────


class _FakeControlSM:
    """Minimal StateManager stand-in exposing only the control surface."""

    def __init__(self, control: str = "running") -> None:
        self._control = control
        self.calls = 0

    async def get_pipeline_control(self, task_id: str) -> str:
        self.calls += 1
        return self._control


@pytest.mark.asyncio
async def test_executor_stop_control_cancels_run():
    sm = _FakeControlSM(control="stopped")
    deps = StageDeps(config=Settings(), state_manager=sm)
    ex = BlueprintExecutor(_registry(), deps)
    out = await ex.execute(load_blueprint_from_string(_LINEAR_BP), DevAITask(blueprint="t-linear"))
    assert out.state == TaskState.CANCELLED
    assert not out.is_failed  # cancelled, not failed
    assert out.stages_completed == []  # stopped before the first stage
    assert sm.calls >= 1


@pytest.mark.asyncio
async def test_executor_runs_normally_when_control_running():
    sm = _FakeControlSM(control="running")
    deps = StageDeps(config=Settings(), state_manager=sm)
    ex = BlueprintExecutor(_registry(), deps)
    out = await ex.execute(load_blueprint_from_string(_LINEAR_BP), DevAITask(blueprint="t-linear"))
    assert out.state == TaskState.COMPLETED
    assert out.stages_completed == ["a", "b"]


@pytest.mark.asyncio
async def test_executor_no_control_surface_is_noop():
    # StateManager without get_pipeline_control (or None) must not break execution.
    deps = StageDeps(config=Settings(), state_manager=object())
    ex = BlueprintExecutor(_registry(), deps)
    out = await ex.execute(load_blueprint_from_string(_LINEAR_BP), DevAITask(blueprint="t-linear"))
    assert out.state == TaskState.COMPLETED


# ── Temporal-mode control: durable Signals ─────────────────────────────────


@pytest.mark.asyncio
async def test_inproc_and_noop_adapters_signal_unsupported():
    # In-process backends don't deliver Signals (control = the Redis flag).
    assert await _inproc().signal("t1", "pause") is False
    assert await NoopWorkflowAdapter().signal("t1", "stop") is False


class _FakeSMControl:
    def __init__(self) -> None:
        self.controls: list = []
        self.redis = None

    async def set_pipeline_control(self, task_id: str, value: str) -> None:
        self.controls.append((task_id, value))


class _FakePipelineSig:
    def __init__(self) -> None:
        self.signals: list = []

    def get_task(self, task_id: str):
        # No live in-memory task — set_run_control still sets the flag + signals,
        # and (for a real run) would reflect the control onto the snapshot.
        return None

    async def signal_run(self, task_id: str, name: str, args=None) -> bool:
        self.signals.append((task_id, name, args))
        return True


@pytest.mark.asyncio
async def test_service_set_run_control_sets_flag_and_signals():
    from devai.pipeline.service import PipelineService

    svc = PipelineService(Settings())
    svc.state_manager = _FakeSMControl()
    svc._pipeline = _FakePipelineSig()  # type: ignore[assignment]
    assert await svc.set_run_control("t1", "stopped") is True
    assert svc.state_manager.controls == [("t1", "stopped")]
    assert svc._pipeline.signals == [("t1", "stop", None)]  # type: ignore[attr-defined]


class _FakeDeleteSM(_FakeSMControl):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[tuple[str, str]] = []

    async def ack_task(self, task_id: str) -> None:
        self.deleted.append(("ack", task_id))

    async def delete_pipeline_task(self, task_id: str) -> None:
        self.deleted.append(("pipeline", task_id))

    async def delete_run(self, task_id: str) -> None:
        self.deleted.append(("legacy", task_id))


@pytest.mark.asyncio
async def test_service_delete_stops_remote_executor_and_keeps_stop_flag():
    from devai.pipeline.service import PipelineService

    svc = PipelineService(Settings())
    svc.state_manager = _FakeDeleteSM()
    svc._pipeline = _FakePipelineSig()  # type: ignore[assignment]

    assert await svc.delete_run("t1") is True
    assert svc.state_manager.controls == [("t1", "stopped")]
    assert svc._pipeline.signals == [("t1", "stop", None)]  # type: ignore[attr-defined]
    assert svc.state_manager.deleted == [("ack", "t1"), ("pipeline", "t1"), ("legacy", "t1")]


@pytest.mark.asyncio
async def test_service_approve_gate_signals_workflow():
    from devai.pipeline.service import PipelineService

    svc = PipelineService(Settings())
    svc._pipeline = _FakePipelineSig()  # type: ignore[assignment]
    await svc.approve_gate("t1", "deploy-release", "approved")
    await svc.approve_gate("t1", "deploy-release", "rejected")
    sigs = svc._pipeline.signals  # type: ignore[attr-defined]
    assert ("t1", "approve", ["deploy-release"]) in sigs
    assert ("t1", "reject", ["deploy-release"]) in sigs
