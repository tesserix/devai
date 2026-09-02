"""Every shipped blueprint, executed end to end against fakes.

The registry-coverage test proves each blueprint's stage keys *exist*; this
one proves the blueprints actually RUN: the DAG resolves, every stage executes
(degrading, not exploding, when a backend is absent), and the run finishes in
a terminal state with an honest verdict rather than silently reporting DONE
having built nothing.

Only the outside world is faked — LLM, SCM and the K8s runtime. The executor,
registry, stages and blueprint YAML are the real ones.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from devai.adapters.llm.base import LLMResponse, LLMUsage
from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import discover_blueprints
from devai.blueprint.registry import StageRegistry, register_defaults
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.lifecycle import assess_work
from devai.pipeline.types import DevAITask, TaskState

REPO_ROOT = Path(__file__).resolve().parents[2]
BLUEPRINTS_DIR = REPO_ROOT / "blueprints"

BLUEPRINTS = sorted(discover_blueprints(BLUEPRINTS_DIR).items())

# Blueprints whose work happens in a backend this harness deliberately doesn't
# stand up. They still have to EXECUTE cleanly; only the "produced output"
# assertion is out of reach here and is covered by the live smoke instead.
NEEDS_RUNTIME = {
    "app-scaffold": "every stage is run_as_job — needs the K8s job runner",
    "sre-monitor": "SRE agents need the cluster + provider clients",
    "weather-agent": "the Weather Agent executes in an isolated K8s Job",
}


class _ScriptedLLM:
    """Answers every agent turn with a well-formed, generic handover."""

    provider_name = "scripted"

    async def generate(self, request: Any) -> LLMResponse:  # noqa: ANN401
        return LLMResponse(
            text='{"summary": "did the work", "findings": [], "assignments": []}',
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        )

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


def _deps() -> StageDeps:
    settings = Settings(
        pipeline_blueprint_dir=str(BLUEPRINTS_DIR),
        crews_dir=str(REPO_ROOT / "crews"),
        specializations_dir=str(REPO_ROOT / "specializations"),
        pipeline_heal_on_failure=False,
    )
    return StageDeps(config=settings, llm=_ScriptedLLM())


@pytest.mark.parametrize("name,blueprint", BLUEPRINTS, ids=[n for n, _ in BLUEPRINTS])
@pytest.mark.asyncio
async def test_blueprint_executes_to_a_terminal_state(name: str, blueprint: Any) -> None:
    deps = _deps()
    executor = BlueprintExecutor(_registry(), deps)
    task = DevAITask(
        intent="add a health endpoint and a test for it",
        blueprint=name,
        repo="tesserix/test-repo",
    )

    result = await executor.execute(blueprint, task)

    # It must not be left mid-flight — a run that never reaches a terminal
    # state is exactly the "spinner forever" the dashboard used to show.
    assert result.state not in (TaskState.PENDING, TaskState.QUEUED), f"{name}: run never left {result.state}"
    # Every stage is accounted for: completed, failed, or explicitly skipped.
    accounted = (
        set(result.stages_completed) | set(result.stages_failed) | set(getattr(result, "stages_skipped", []) or [])
    )
    declared = {s.name for s in blueprint.stages}
    assert declared - accounted == set() or result.state in (
        TaskState.STAGE_FAILED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    ), f"{name}: stages never ran: {sorted(declared - accounted)}"


@pytest.mark.parametrize("name,blueprint", BLUEPRINTS, ids=[n for n, _ in BLUEPRINTS])
@pytest.mark.asyncio
async def test_blueprint_produces_work_when_a_model_is_available(name: str, blueprint: Any) -> None:
    """With a working LLM, a run must hand over real output.

    The composer's "run finished DONE but nothing happened" bug was exactly
    this: every stage degraded to a stub and the run still reported success.
    """
    if name in NEEDS_RUNTIME:
        pytest.skip(NEEDS_RUNTIME[name])
    deps = _deps()
    task = DevAITask(
        intent="add a health endpoint and a test for it",
        blueprint=name,
        repo="tesserix/test-repo",
    )

    result = await BlueprintExecutor(_registry(), deps).execute(blueprint, task)

    produced, reason = assess_work(result)
    assert produced, f"{name}: {reason}"


def _registry() -> StageRegistry:
    reg = StageRegistry()
    register_defaults(reg)
    return reg
