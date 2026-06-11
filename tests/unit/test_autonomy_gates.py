"""Autonomy + gate + resilience tests.

The contract the user experience depends on:
  - autonomy=full/auto → static gates self-approve; runs flow end-to-end.
  - autonomy=gated → static gates pause for the human decision key.
  - plan_approval (smart gate) pauses ONLY for ambiguous intents, with a
    rich approval request (questions/plan/tech) registered as a dynamic gate.
  - transient stage failures retry with backoff before on_failure applies.
"""

from __future__ import annotations

import asyncio

import pytest

from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import Blueprint, StageSpec
from devai.blueprint.registry import StageRegistry
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


class _SM:
    def __init__(self):
        self.redis = _FakeRedis()

    async def get_pipeline_control(self, task_id):
        return None


class _Cfg:
    pipeline_label = "x"
    pipeline_default_autonomy = "auto"
    pipeline_stage_retries = 1


class _OkStage(PipelineStage):
    def __init__(self, *a, **kw):
        pass

    def name(self):
        return "ok"

    async def execute(self, task):
        return StageResult(message="done")


def _executor(sm=None, registry=None):
    reg = registry or StageRegistry()
    if not reg.has("noop_ok"):
        reg.register("noop_ok", lambda deps, cfg: _OkStage())
    deps = StageDeps(config=_Cfg(), state_manager=sm or _SM())
    return BlueprintExecutor(reg, deps), reg


def _bp(stages):
    return Blueprint(name="t", description="", stages=stages)


# ──────────────────────────────────────────────────────────────────────
# Static gates × autonomy
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_static_gate_self_approves_under_auto_autonomy():
    sm = _SM()
    ex, _ = _executor(sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)

    assert task.state == TaskState.COMPLETED
    assert "deploy" in task.stages_completed
    assert sm.redis.data[f"devai:pipeline:gate:{task.id}:deploy"] == "approved"
    assert sm.redis.data[f"devai:pipeline:gate:{task.id}:deploy:approver"] == "autonomy:full"
    assert any("auto-approved" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_static_gate_blocks_until_decision_when_gated():
    sm = _SM()
    ex, _ = _executor(sm)
    ex._GATE_POLL_SECONDS = 0.01
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.agent_context["autonomy"] = "gated"
    key = f"devai:pipeline:gate:{task.id}:deploy"

    async def approve_later():
        await asyncio.sleep(0.05)
        await sm.redis.set(key, "approved")

    approver = asyncio.create_task(approve_later())
    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)
    await approver

    assert task.state == TaskState.COMPLETED
    assert "deploy" in task.stages_completed
    assert any("waiting for human approval" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_static_gate_rejection_cancels_run():
    sm = _SM()
    ex, _ = _executor(sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.agent_context["autonomy"] = "gated"
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:deploy", "rejected")

    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)
    assert task.state == TaskState.CANCELLED
    assert "rejected at gate" in (task.error or "")
    assert "deploy" not in task.stages_completed


# ──────────────────────────────────────────────────────────────────────
# Transient-failure retries
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_retries_transient_failure_then_succeeds():
    attempts = {"n": 0}

    class _Flaky(PipelineStage):
        def name(self):
            return "flaky"

        async def execute(self, task):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("LLM hiccup")
            return StageResult(message="recovered")

    reg = StageRegistry()
    reg.register("flaky", lambda deps, cfg: _Flaky())
    ex, _ = _executor(registry=reg)
    task = DevAITask(intent="x", blueprint="t", repo="o/r")
    await ex.execute(_bp([StageSpec(name="s1", stage="flaky")]), task)

    assert attempts["n"] == 2
    assert task.state == TaskState.COMPLETED
    assert "s1" in task.stages_completed
    assert any("retrying after failure" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_stage_fails_after_retries_exhausted():
    class _AlwaysBoom(PipelineStage):
        def name(self):
            return "boom"

        async def execute(self, task):
            raise RuntimeError("permanent")

    reg = StageRegistry()
    reg.register("boom", lambda deps, cfg: _AlwaysBoom())
    ex, _ = _executor(registry=reg)
    task = DevAITask(intent="x", blueprint="t", repo="o/r")
    await ex.execute(_bp([StageSpec(name="s1", stage="boom")]), task)

    assert task.is_failed
    assert "s1" in task.stages_failed


# ──────────────────────────────────────────────────────────────────────
# Smart plan approval
# ──────────────────────────────────────────────────────────────────────


class _FakeLLM:
    provider_name = "anthropic"

    def __init__(self, text):
        self._text = text

    async def generate(self, request):
        text = self._text

        class _R:
            pass

        r = _R()
        r.text = text
        return r


def _plan_stage(sm=None, llm=None, autonomy=None):
    from devai.pipeline.stages.lifecycle import plan_approval_stage

    deps = StageDeps(config=_Cfg(), state_manager=sm, llm=llm)
    stage = plan_approval_stage(deps, {"__stage_name": "plan-approval"})
    task = DevAITask(intent="build me the petstore with cart and login", blueprint="alm-pipeline", repo="o/r")
    if autonomy:
        task.agent_context["autonomy"] = autonomy
    return stage, task


@pytest.mark.asyncio
async def test_plan_approval_skips_when_full():
    stage, task = _plan_stage(autonomy="full")
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto"


@pytest.mark.asyncio
async def test_plan_approval_clear_intent_proceeds_without_pause():
    stage, task = _plan_stage(sm=_SM(), llm=_FakeLLM("CLEAR"))
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto-clear"
    assert task.agent_context.get("dynamic_gates") is None


@pytest.mark.asyncio
async def test_plan_approval_ambiguous_pauses_with_rich_request_then_approves():
    sm = _SM()
    stage, task = _plan_stage(
        sm=sm,
        llm=_FakeLLM("AMBIGUOUS\nWhich database should the petstore use?\nIs authentication social or email/password?"),
    )
    task.agent_context["detected_tech_stack"] = "nextjs+fastapi"
    task.agent_context["engineering_manager_output"] = {"technical_plan": "Build API first, then UI."}
    task.epic_issue_number = 42
    # Pre-approve so the wait loop returns immediately.
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:plan-approval", "approved")

    result = await stage.execute(task)

    assert result.data["plan_approved"] == "human"
    gates = task.agent_context["dynamic_gates"]
    assert len(gates) == 1
    g = gates[0]
    assert g["kind"] == "plan_approval"
    assert g["tech_stack"] == "nextjs+fastapi"
    assert g["plan_summary"] == "Build API first, then UI."
    assert g["epic_issue_number"] == 42
    assert len(g["questions"]) == 2
    assert "database" in g["questions"][0]


@pytest.mark.asyncio
async def test_plan_approval_rejection_cancels():
    sm = _SM()
    stage, task = _plan_stage(sm=sm, llm=_FakeLLM("AMBIGUOUS\nWhat tech stack?"))
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:plan-approval", "rejected")
    result = await stage.execute(task)
    assert result.next_state == TaskState.CANCELLED
    assert "rejected" in (task.error or "")


@pytest.mark.asyncio
async def test_plan_approval_no_llm_never_blocks():
    stage, task = _plan_stage(sm=_SM(), llm=None)
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto-clear"
