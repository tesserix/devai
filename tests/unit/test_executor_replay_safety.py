"""A stage whose outcome is uncertain must never be re-run.

ADR-0004: once kagent may have accepted a dispatch, replaying it can execute
side-effecting tools twice. The stage raises
``KagentDispatchOutcomeUncertain``, but the executor sat above it catching
broad ``Exception`` — transient retries plus heal rounds would re-dispatch the
very task that may already be running.
"""

from __future__ import annotations

import json

import pytest

from devai.agentic.kagent_client import KagentDispatchOutcomeUncertain
from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import Blueprint, StageSpec
from devai.blueprint.registry import StageRegistry
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, TaskState


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
    pipeline_stage_retries = 1  # a retryable failure WOULD run twice
    pipeline_heal_on_failure = True
    pipeline_heal_attempts = 1
    pipeline_stage_inactivity_grace = 240
    pipeline_stage_hard_cap_multiplier = 100


class _HealingLLM:
    """Always votes to re-run the stage."""

    provider_name = "fake"

    async def generate(self, request):
        class _R:
            text = json.dumps({"diagnosis": "transient", "action": "retry", "guidance": "try again"})

        return _R()


class _Counting(PipelineStage):
    def __init__(self, exc):
        self.exc = exc
        self.attempts = 0

    def name(self):
        return "counting"

    async def execute(self, task):
        self.attempts += 1
        raise self.exc


def _run(stage):
    reg = StageRegistry()
    reg.register("work", lambda deps, cfg: stage)
    deps = StageDeps(config=_Cfg(), state_manager=_SM(), llm=_HealingLLM())
    ex = BlueprintExecutor(reg, deps)
    bp = Blueprint(name="t", description="", stages=[StageSpec(name="build", stage="work")])
    return ex.execute(bp, DevAITask(intent="ship", blueprint="t", repo="o/r"))


@pytest.mark.asyncio
async def test_uncertain_outcome_is_never_replayed():
    stage = _Counting(KagentDispatchOutcomeUncertain("may have been accepted"))
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    reg = StageRegistry()
    reg.register("work", lambda deps, cfg: stage)
    deps = StageDeps(config=_Cfg(), state_manager=_SM(), llm=_HealingLLM())
    ex = BlueprintExecutor(reg, deps)
    await ex.execute(Blueprint(name="t", description="", stages=[StageSpec(name="build", stage="work")]), task)

    assert stage.attempts == 1
    assert task.state == TaskState.STAGE_FAILED
    assert "build" in task.stages_failed


@pytest.mark.asyncio
async def test_ordinary_failure_still_retries():
    """Proves the config above would otherwise re-run the stage."""
    stage = _Counting(RuntimeError("flaky"))
    await _run(stage)
    assert stage.attempts > 1


def test_uncertain_error_is_marked_retry_unsafe():
    assert KagentDispatchOutcomeUncertain("x").retry_unsafe is True
