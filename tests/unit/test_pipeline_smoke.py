"""Smoke tests for the Fiber-style blueprint runtime.

These tests exercise the foundation end-to-end *without* standing up
Redis, Postgres, the SCM client, or any LLM provider. The adapter stages
detect missing dependencies and return stub StageResults, so an entire
blueprint can run in seconds in CI.

What we cover:
1. YAML loader parses each shipped blueprint.
2. Every stage referenced by a shipped blueprint is registered.
3. Condition evaluator handles task.has_pr / negation / and / or.
4. Topo sort orders dependencies correctly.
5. Pipeline.run_once executes alm-pipeline end-to-end against stub deps
   and lands the task in COMPLETED state.
6. on_failure=continue lets the pipeline keep going after a stage failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devai.blueprint import (
    BlueprintExecutor,
    StageRegistry,
    discover_blueprints,
    load_blueprint_from_string,
    register_defaults,
)
from devai.blueprint.conditions import evaluate
from devai.blueprint.executor import _topo_sort
from devai.blueprint.loader import BlueprintLoadError, StageSpec
from devai.pipeline import (
    DevAITask,
    Pipeline,
    PipelineStage,
    StageDeps,
    StageResult,
    TaskState,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BLUEPRINTS_DIR = REPO_ROOT / "blueprints"


# ─── Settings stub — no env vars required ──────────────────────────────


class _StubSettings:
    pipeline_label = "devai:automate"
    redis_url = "redis://localhost:6379"


def _deps() -> StageDeps:
    return StageDeps(config=_StubSettings())  # type: ignore[arg-type]


# ─── Loader ──────────────────────────────────────────────────────────


def test_loader_parses_all_shipped_blueprints():
    blueprints = discover_blueprints(BLUEPRINTS_DIR)
    expected = {"alm-pipeline", "sre-monitor", "pr-review", "security-scan"}
    assert expected.issubset(set(blueprints.keys())), f"missing: {expected - blueprints.keys()}"


def test_loader_rejects_duplicate_stage_names():
    text = """
name: bad
description: dup
stages:
  - name: a
    stage: noop
  - name: a
    stage: noop
"""
    with pytest.raises(BlueprintLoadError, match="duplicate"):
        load_blueprint_from_string(text)


def test_loader_rejects_unknown_dep():
    text = """
name: bad-dep
description: bad dep
stages:
  - name: a
    stage: noop
    depends_on: [does-not-exist]
"""
    with pytest.raises(BlueprintLoadError, match="depends on unknown stage"):
        load_blueprint_from_string(text)


def test_loader_parses_durations():
    text = """
name: dur
description: durations
stages:
  - name: fast
    stage: noop
    timeout: 500ms
  - name: medium
    stage: noop
    timeout: 30s
    depends_on: [fast]
  - name: slow
    stage: noop
    timeout: 15m
    depends_on: [medium]
"""
    bp = load_blueprint_from_string(text)
    assert bp.stages[0].timeout_seconds == pytest.approx(0.5)
    assert bp.stages[1].timeout_seconds == pytest.approx(30.0)
    assert bp.stages[2].timeout_seconds == pytest.approx(900.0)


# ─── Registry ────────────────────────────────────────────────────────


def test_registry_covers_every_shipped_blueprint():
    reg = StageRegistry()
    register_defaults(reg)
    known = set(reg.known_stages())
    blueprints = discover_blueprints(BLUEPRINTS_DIR)
    for name, bp in blueprints.items():
        for spec in bp.stages:
            assert spec.stage in known, f"{name}: stage {spec.stage!r} missing from registry"


# ─── Conditions ──────────────────────────────────────────────────────


def _task() -> DevAITask:
    return DevAITask(intent="hello", blueprint="x", repo="org/repo")


def test_condition_no_expr_is_true():
    assert evaluate(None, _task()) is True
    assert evaluate("", _task()) is True


def test_condition_task_has_pr():
    t = _task()
    assert evaluate("task.has_pr", t) is False
    t.pr_number = 7
    assert evaluate("task.has_pr", t) is True


def test_condition_negation_both_forms():
    t = _task()
    assert evaluate("!task.has_pr", t) is True
    assert evaluate("! task.has_pr", t) is True
    t.pr_number = 1
    assert evaluate("!task.has_pr", t) is False


def test_condition_and_or():
    t = _task()
    t.pr_number = 1
    t.epic_issue_number = 5
    assert evaluate("task.has_pr and task.has_epic", t) is True
    assert evaluate("task.has_pr and task.has_sandbox", t) is False
    assert evaluate("task.has_sandbox or task.has_pr", t) is True


def test_condition_unknown_key_fail_open():
    assert evaluate("task.never_heard_of_it", _task()) is True


def test_condition_state_lookup():
    t = _task()
    t.transition(TaskState.PLANNING)
    assert evaluate("state.planning", t) is True
    assert evaluate("state.deploying", t) is False


# ─── Topo sort ───────────────────────────────────────────────────────


def test_topo_sort_simple_chain():
    specs = [
        StageSpec(name="a", stage="noop"),
        StageSpec(name="b", stage="noop", depends_on=["a"]),
        StageSpec(name="c", stage="noop", depends_on=["b"]),
    ]
    levels = _topo_sort(specs)
    assert [[s.name for s in level] for level in levels] == [["a"], ["b"], ["c"]]


def test_topo_sort_parallel_level():
    specs = [
        StageSpec(name="root", stage="noop"),
        StageSpec(name="x", stage="noop", depends_on=["root"]),
        StageSpec(name="y", stage="noop", depends_on=["root"]),
        StageSpec(name="z", stage="noop", depends_on=["root"]),
        StageSpec(name="join", stage="noop", depends_on=["x", "y", "z"]),
    ]
    levels = _topo_sort(specs)
    assert [s.name for s in levels[0]] == ["root"]
    assert {s.name for s in levels[1]} == {"x", "y", "z"}
    assert [s.name for s in levels[2]] == ["join"]


def test_topo_sort_detects_cycles():
    from devai.blueprint.executor import BlueprintExecutionError

    specs = [
        StageSpec(name="a", stage="noop", depends_on=["b"]),
        StageSpec(name="b", stage="noop", depends_on=["a"]),
    ]
    with pytest.raises(BlueprintExecutionError, match="cycle"):
        _topo_sort(specs)


# ─── End-to-end: alm-pipeline through stub adapters ─────────────────


@pytest.mark.asyncio
async def test_alm_pipeline_runs_end_to_end_in_dry_run():
    """The full alm-pipeline blueprint executes against StageDeps with no
    SCM client. Every agent adapter detects missing deps, returns a stub
    StageResult, and the task lands in COMPLETED."""
    pipeline = Pipeline(_deps(), blueprint_dir=BLUEPRINTS_DIR)
    pipeline.load_blueprints()

    task = DevAITask(intent="add /health endpoint", blueprint="alm-pipeline", repo="tesserix/devai")
    await pipeline.run_once(task)

    assert task.state == TaskState.COMPLETED, f"task ended in {task.state}: {task.error}"
    # Every stage in the blueprint should have either completed or been skipped.
    bp = pipeline.get_blueprint("alm-pipeline")
    accounted = set(task.stages_completed) | set(task.stages_skipped) | set(task.stages_failed)
    assert accounted == bp.stage_names()


@pytest.mark.asyncio
async def test_sre_monitor_runs_parallel_level():
    """The SRE monitor blueprint has a parallel fan-out level (5 monitors).
    Verify they all run and the correlator gets a chance to consume them."""
    pipeline = Pipeline(_deps(), blueprint_dir=BLUEPRINTS_DIR)
    pipeline.load_blueprints()

    task = DevAITask(intent="cluster sweep", blueprint="sre-monitor")
    await pipeline.run_once(task)

    assert task.state == TaskState.COMPLETED
    # Correlate should always run, even when the monitors stub out.
    assert "correlate" in task.stages_completed


# ─── on_failure semantics ────────────────────────────────────────────


class _AlwaysFailStage(PipelineStage):
    def name(self) -> str:
        return "always_fail"

    async def execute(self, task: DevAITask) -> StageResult:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_on_failure_continue_keeps_going():
    reg = StageRegistry()
    register_defaults(reg)
    reg.register("always_fail", lambda deps, cfg: _AlwaysFailStage())

    blueprint = load_blueprint_from_string(
        """
name: continue-test
description: on_failure=continue
stages:
  - name: bad
    stage: always_fail
    on_failure: continue
  - name: good
    stage: noop
    depends_on: [bad]
"""
    )
    executor = BlueprintExecutor(reg, _deps())
    task = DevAITask(intent="x", blueprint="continue-test")
    await executor.execute(blueprint, task)

    assert task.state == TaskState.COMPLETED
    assert "bad" in task.stages_failed
    assert "good" in task.stages_completed


@pytest.mark.asyncio
async def test_on_failure_stop_short_circuits():
    reg = StageRegistry()
    register_defaults(reg)
    reg.register("always_fail", lambda deps, cfg: _AlwaysFailStage())

    blueprint = load_blueprint_from_string(
        """
name: stop-test
description: on_failure=stop
stages:
  - name: bad
    stage: always_fail
  - name: good
    stage: noop
    depends_on: [bad]
"""
    )
    executor = BlueprintExecutor(reg, _deps())
    task = DevAITask(intent="x", blueprint="stop-test")
    await executor.execute(blueprint, task)

    assert task.is_failed
    assert "bad" in task.stages_failed
    assert "good" not in task.stages_completed


# ─── Resumption ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_stages_are_skipped_on_replay():
    reg = StageRegistry()
    register_defaults(reg)
    blueprint = load_blueprint_from_string(
        """
name: replay
description: replay
stages:
  - name: a
    stage: noop
  - name: b
    stage: noop
    depends_on: [a]
"""
    )
    executor = BlueprintExecutor(reg, _deps())
    task = DevAITask(intent="x", blueprint="replay")
    task.stages_completed = ["a"]  # simulate prior run

    await executor.execute(blueprint, task)
    # 'a' is skipped (already done); 'b' runs fresh.
    skipped_or_done_a = "a" in task.stages_skipped or task.stages_completed.count("a") <= 1
    assert skipped_or_done_a
    assert "b" in task.stages_completed
