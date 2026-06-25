"""Contract tests for the collaboration patterns (RecursiveMAS shapes).

Each pattern is exercised with tiny fake agents over a real AgentDispatcher
(InlineBackend), asserting the orchestration shape: sequential threads outputs,
mixture aggregates concurrently, deliberation loops until the critic accepts,
distillation escalates on uncertainty.
"""

from __future__ import annotations

import pytest

from devai.agentruntime import (
    AgentDispatcher,
    AgentResult,
    RunContext,
    deliberation,
    distillation,
    mixture,
    sequential,
)
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask


def _deps() -> StageDeps:
    return StageDeps(config=None)


def _task(**kw) -> DevAITask:
    return DevAITask(intent=kw.pop("intent", "do it"), **kw)


class _Echo:
    """Writes `{marker: value}` under its output_key and echoes what it saw."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.output_key = f"{name}_output"
        self._value = value

    async def run(self, ctx: RunContext) -> AgentResult:
        saw = sorted(k for k in ctx.agent_context if k.endswith("_output"))
        return AgentResult(handover={"value": self._value, "saw": saw}, output_key=self.output_key)


@pytest.mark.asyncio
async def test_sequential_threads_prior_outputs():
    dispatcher = AgentDispatcher(_deps())
    a, b, c = _Echo("a", "1"), _Echo("b", "2"), _Echo("c", "3")
    task = _task()

    results = await sequential(dispatcher, [a, b, c], task)

    assert [r.handover["value"] for r in results] == ["1", "2", "3"]
    # b saw a's output; c saw a's + b's.
    assert results[1].handover["saw"] == ["a_output"]
    assert results[2].handover["saw"] == ["a_output", "b_output"]
    # final bag carries every output.
    assert set(task.agent_context) >= {"a_output", "b_output", "c_output"}


@pytest.mark.asyncio
async def test_mixture_aggregates_all_handovers():
    dispatcher = AgentDispatcher(_deps())
    result = await mixture(dispatcher, [_Echo("x", "X"), _Echo("y", "Y")], _task())

    assert set(result.handover) == {"x_output", "y_output"}
    assert result.handover["x_output"]["value"] == "X"


@pytest.mark.asyncio
async def test_mixture_custom_aggregator():
    dispatcher = AgentDispatcher(_deps())

    async def _agg(results, task):
        return AgentResult(handover={"count": len([r for r in results if not r.error])})

    result = await mixture(dispatcher, [_Echo("x", "X"), _Echo("y", "Y")], _task(), aggregate=_agg)
    assert result.handover == {"count": 2}


@pytest.mark.asyncio
async def test_deliberation_loops_until_critic_approves():
    rounds = {"n": 0}

    class _Actor:
        name = "actor"
        output_key = "actor_output"

        async def run(self, ctx):  # noqa: ANN001
            rounds["n"] += 1
            return AgentResult(handover={"draft": rounds["n"]}, output_key=self.output_key)

    class _Critic:
        name = "critic"
        output_key = "critic_output"

        async def run(self, ctx):  # noqa: ANN001
            # Approves only once the actor has produced its 2nd draft.
            draft = ctx.agent_context.get("actor_output", {}).get("draft", 0)
            return AgentResult(handover={"approved": draft >= 2}, output_key=self.output_key)

    result = await deliberation(AgentDispatcher(_deps()), _Actor(), _Critic(), _task(), max_rounds=5)
    assert rounds["n"] == 2
    assert result.handover["_deliberation_approved"] is True


@pytest.mark.asyncio
async def test_deliberation_gives_up_after_max_rounds():
    class _Actor:
        name = "actor"
        output_key = "actor_output"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(handover={"draft": 1}, output_key=self.output_key)

    class _Never:
        name = "critic"
        output_key = "critic_output"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(handover={"approved": False}, output_key=self.output_key)

    result = await deliberation(AgentDispatcher(_deps()), _Actor(), _Never(), _task(), max_rounds=3)
    assert result.handover["_deliberation_approved"] is False


@pytest.mark.asyncio
async def test_distillation_escalates_on_learner_error():
    class _Learner:
        name = "learner"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(ok=False, error="not sure", handover={})

    class _Expert:
        name = "expert"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(handover={"answer": "definitive"})

    result = await distillation(AgentDispatcher(_deps()), _Learner(), _Expert(), _task())
    assert result.handover["answer"] == "definitive"
    assert result.handover["_escalated_from"] == "learner"


@pytest.mark.asyncio
async def test_distillation_keeps_learner_when_confident():
    class _Learner:
        name = "learner"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(handover={"answer": "cheap+correct"})

    class _Expert:
        name = "expert"

        async def run(self, ctx):  # noqa: ANN001 — must not be reached
            raise AssertionError("expert should not run when the learner is confident")

    result = await distillation(AgentDispatcher(_deps()), _Learner(), _Expert(), _task())
    assert result.handover["answer"] == "cheap+correct"
    assert "_escalated_from" not in result.handover
