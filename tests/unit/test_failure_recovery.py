"""Autonomous failure recovery (the executor's recovery agent).

The contract: when a stage exhausts its transient retries, a recovery agent
reviews the failure BEFORE on_failure semantics apply —

  - action=retry    → corrective guidance lands in agent_context and the
                      stage re-runs; success continues the pipeline.
  - action=ask_user → a dynamic heal gate pauses the run for the human
                      (self-approved under autonomy=full).
  - action=abort / no LLM / unusable response → the failure stands and the
                      blueprint's on_failure semantics apply unchanged.
"""

from __future__ import annotations

import json

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
    pipeline_stage_retries = 0  # isolate recovery from transient retries
    pipeline_heal_on_failure = True
    pipeline_heal_attempts = 1


class _FakeLLM:
    """Scripted recovery-specialist responses."""

    provider_name = "fake"

    def __init__(self, decision: dict):
        self.decision = decision
        self.calls: list[str] = []

    async def generate(self, request):
        self.calls.append(request.messages[0].content)

        class _R:
            text = json.dumps(self.decision)

        return _R()


class _FlakyStage(PipelineStage):
    """Fails until it sees recovery guidance on the task."""

    def __init__(self, task_ctx_key: str):
        self.key = task_ctx_key
        self.attempts = 0

    def name(self):
        return "flaky"

    async def execute(self, task):
        self.attempts += 1
        if not task.agent_context.get(self.key):
            raise RuntimeError("stories response unparseable")
        return StageResult(message="fixed on retry")


class _AlwaysFails(PipelineStage):
    def name(self):
        return "broken"

    async def execute(self, task):
        raise RuntimeError("permanently broken")


def _executor(llm=None, sm=None, stage_factory=None, autonomy="auto"):
    reg = StageRegistry()
    reg.register("work", stage_factory or (lambda deps, cfg: _AlwaysFails()))
    cfg = _Cfg()
    cfg.pipeline_default_autonomy = autonomy
    deps = StageDeps(config=cfg, state_manager=sm or _SM(), llm=llm)
    return BlueprintExecutor(reg, deps)


def _bp(**spec_kw):
    return Blueprint(
        name="t", description="", stages=[StageSpec(name="build", stage="work", **spec_kw)]
    )


@pytest.mark.asyncio
async def test_retry_decision_reruns_stage_with_guidance():
    llm = _FakeLLM(
        {"diagnosis": "LLM wrapped JSON in fences", "action": "retry", "guidance": "strip markdown fences"}
    )
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, stage_factory=lambda deps, cfg: stage)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED
    assert "build" in task.stages_completed
    assert stage.attempts == 2  # failed once, healed, succeeded
    heal = task.agent_context["heal:build"]
    assert heal["guidance"] == "strip markdown fences"
    assert any(e.stage == "heal:build" and e.agent == "recovery_specialist" for e in task.stage_events)
    # the failure prompt reached the recovery agent
    assert "stories response unparseable" in llm.calls[0]


@pytest.mark.asyncio
async def test_abort_decision_lets_failure_stand():
    llm = _FakeLLM({"diagnosis": "credentials revoked", "action": "abort", "guidance": ""})
    ex = _executor(llm=llm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert "build" in task.stages_failed
    assert any(e.stage == "heal:build" and "abandoned" in (e.error or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_no_llm_falls_back_to_on_failure_semantics():
    ex = _executor(llm=None)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(on_failure="continue"), task)

    # heal degraded; on_failure=continue keeps the old behavior
    assert task.state == TaskState.COMPLETED
    assert "build" in task.stages_failed


@pytest.mark.asyncio
async def test_recovery_rounds_are_bounded():
    llm = _FakeLLM({"diagnosis": "try again", "action": "retry", "guidance": "same thing"})
    ex = _executor(llm=llm)  # stage always fails, heal_attempts=1
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert len(llm.calls) == 1  # exactly one recovery round, no infinite loop


@pytest.mark.asyncio
async def test_ask_user_self_approves_under_full_autonomy():
    llm = _FakeLLM(
        {
            "diagnosis": "needs decision",
            "action": "ask_user",
            "guidance": "use mock payments",
            "user_message": "OK to mock payments?",
        }
    )
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, stage_factory=lambda deps, cfg: stage, autonomy="full")
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED  # no pause under full autonomy
    assert not task.agent_context.get("dynamic_gates")


@pytest.mark.asyncio
async def test_ask_user_raises_dynamic_gate_and_approval_reruns():
    llm = _FakeLLM(
        {
            "diagnosis": "schema choice unclear",
            "action": "ask_user",
            "guidance": "use a flat schema",
            "user_message": "Flatten the schema?",
        }
    )
    sm = _SM()
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, sm=sm, stage_factory=lambda deps, cfg: stage)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    # pre-write the human's approval so the poll resolves immediately
    sm.redis.data[f"devai:pipeline:gate:{task.id}:heal-build"] = "approved"
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED
    gates = task.agent_context["dynamic_gates"]
    assert gates and gates[0]["gate"] == "heal-build"
    assert gates[0]["kind"] == "heal_approval"
    assert gates[0]["questions"] == ["Flatten the schema?"]


@pytest.mark.asyncio
async def test_ask_user_rejection_lets_failure_stand():
    llm = _FakeLLM(
        {"diagnosis": "d", "action": "ask_user", "guidance": "g", "user_message": "ok?"}
    )
    sm = _SM()
    ex = _executor(llm=llm, sm=sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    sm.redis.data[f"devai:pipeline:gate:{task.id}:heal-build"] = "rejected"
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert "build" in task.stages_failed


@pytest.mark.asyncio
async def test_heal_disabled_via_config():
    llm = _FakeLLM({"diagnosis": "x", "action": "retry", "guidance": "y"})
    reg = StageRegistry()
    reg.register("work", lambda deps, cfg: _AlwaysFails())
    cfg = _Cfg()
    cfg.pipeline_heal_on_failure = False
    deps = StageDeps(config=cfg, state_manager=_SM(), llm=llm)
    ex = BlueprintExecutor(reg, deps)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert llm.calls == []  # recovery never consulted
