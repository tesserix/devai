"""The generic ``ForEachStage`` fan-out / map primitive (Phase 0).

It's the framework behind multi-story implementation but ALM- and agent-
agnostic: any blueprint can map any agent over any handover list. Proves the
loop advances the index, shares one branch / PR across items, 0–1 items → a
single dispatch, and that the ALM ``implement_code`` stage is a thin wiring over
it (so the per-story loop the blueprint port dropped is back — generically)."""

from __future__ import annotations

import pytest

from devai.agentruntime import AgentResult, RunContext
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.flow import ForEachStage
from devai.pipeline.types import DevAITask


class _RecordingAgent:
    """Records the per-item index it was handed and returns one shared branch +
    PR (as the idempotent-PR loop would)."""

    name = "worker"

    def __init__(self) -> None:
        self.indices: list[int | None] = []

    async def run(self, ctx: RunContext) -> AgentResult:
        idx = ctx.extra_context.get("active_story_index")
        self.indices.append(idx)
        return AgentResult(
            ok=True,
            output_key="worker",
            handover={
                "branch_name": "story/epic-integration",
                "pr_number": 7,
                "implementation_summary": f"did item {idx}",
            },
        )


def _deps() -> StageDeps:
    return StageDeps(config=None, scm=None, state_manager=None, llm=None)


def _task(items: list) -> DevAITask:
    task = DevAITask(intent="build the app", repo="tesserix/x")
    task.dry_run = True
    task.agent_context["stories"] = items
    return task


def _no_trial_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(deps, task, *, trial_gate, stage_name):  # noqa: ANN001
        return deps.config, deps.scm

    monkeypatch.setattr("devai.pipeline.stages.flow.resolve_principal_run", _fake)


def _stage(agent: _RecordingAgent) -> ForEachStage:
    return ForEachStage(_deps(), agent=agent, output_key="worker", name="implement_code", items_key="stories")


@pytest.mark.asyncio
async def test_maps_agent_over_every_item_on_one_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_trial_gate(monkeypatch)
    agent = _RecordingAgent()
    task = _task([{"title": "auth"}, {"title": "profile"}, {"title": "billing"}])

    result = await _stage(agent).execute(task)

    assert agent.indices == [0, 1, 2]  # one pass per item, index advancing
    assert task.branch_name == "story/epic-integration"  # all share one branch
    assert task.pr_number == 7  # …and one PR
    assert result.data["pr_number"] == 7
    assert result.data["worker"]["stories_processed"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], [{"title": "only"}]])
async def test_zero_or_one_item_is_a_single_dispatch(monkeypatch: pytest.MonkeyPatch, items: list) -> None:
    _no_trial_gate(monkeypatch)
    agent = _RecordingAgent()

    await _stage(agent).execute(_task(items))

    assert agent.indices == [None]  # back-compat: ONE dispatch, no per-item index override


def test_alm_implement_code_is_a_thin_for_each() -> None:
    """The ALM implement stage is just ForEachStage over `stories` with the
    senior developer + the PR contract — not a bespoke pipeline-specific class."""
    from devai.pipeline.stages.alm import implement_code_stage

    stage = implement_code_stage(_deps(), {})
    assert isinstance(stage, ForEachStage)
    assert stage._items_key == "stories"
    assert stage._validator is not None  # the pull-request output contract
