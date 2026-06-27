"""Phase 4.2 — per-agent memory recall.

The generic AgentStage now recalls what THIS agent learned on THIS repo and
merges it on top of the run-level memory_context, so the agents that make the
learnable mistakes get role-relevant memory (the run-level injection reached
mostly reviewers). Best-effort: no adapter / a miss leaves the run-level
context untouched."""

from __future__ import annotations

import pytest

from devai.agentruntime import AgentResult
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.agent_stage import AgentStage
from devai.pipeline.types import DevAITask


class _Record:
    def __init__(self, content: str) -> None:
        self.content = content


class _Memory:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records
        self.seen: dict = {}

    async def semantic_search(self, query, *, k=5, agent=None, repo=None, memory_type=None):  # noqa: ANN001
        self.seen = {"query": query, "agent": agent, "repo": repo}
        return list(self._records)


class _Agent:
    name = "tech_detector"

    async def run(self, ctx):  # noqa: ANN001
        return AgentResult(handover={})


def _stage(memory) -> AgentStage:  # noqa: ANN001
    deps = StageDeps(config=None, scm=None, state_manager=None, llm=None, memory=memory)
    return AgentStage(deps, name="detect_tech_stack", agent=_Agent(), output_key="tech_detector")


@pytest.mark.asyncio
async def test_recalls_memory_for_this_agent_and_repo() -> None:
    mem = _Memory([_Record("repo uses Next.js app router")])
    out = await _stage(mem)._recall_agent_memory(DevAITask(intent="build app", repo="tesserix/x"))

    assert out == {"memory_context": "- repo uses Next.js app router"}
    assert mem.seen["agent"] == "tech_detector"  # scoped to THIS agent (the maker)
    assert mem.seen["repo"] == "tesserix/x"


@pytest.mark.asyncio
async def test_merges_on_top_of_run_level_memory() -> None:
    mem = _Memory([_Record("avoid src/ layout here")])
    task = DevAITask(intent="x", repo="tesserix/x")
    task.agent_context["memory_context"] = "run-level note"
    out = await _stage(mem)._recall_agent_memory(task)

    assert out == {"memory_context": "- avoid src/ layout here\nrun-level note"}


@pytest.mark.asyncio
async def test_no_memory_adapter_returns_none() -> None:
    assert await _stage(None)._recall_agent_memory(DevAITask(intent="x", repo="t/x")) is None


@pytest.mark.asyncio
async def test_no_records_leaves_run_level_untouched() -> None:
    out = await _stage(_Memory([]))._recall_agent_memory(DevAITask(intent="x", repo="t/x"))
    assert out is None  # no agent-specific memory → don't override the run-level context
