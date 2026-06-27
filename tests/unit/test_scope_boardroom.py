"""Phase 2 — scope-aware decomposition + binding the boardroom.

Proves the analyst stage derives a scope signal (which sizes decomposition and
auto-fires the boardroom), the stories prompt is sized to scope, and the
boardroom's agreed decision is surfaced so it reaches the planner + implementer
instead of being a string nothing reads."""

from __future__ import annotations

import pytest

from devai.agentruntime.agent import DEFAULT_SURFACE_KEYS
from devai.agents.product_director import _scope_story_guidance
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.alm import _derive_scope
from devai.pipeline.stages.flow import ClarificationGateStage
from devai.pipeline.types import DevAITask


def test_derive_scope_sizes_by_requirement_count() -> None:
    assert _derive_scope({"analyzed_requirements": [1, 2]}) == {"scope_size": "small", "scope_large": False}
    assert _derive_scope({"analyzed_requirements": list(range(4))})["scope_size"] == "medium"
    assert _derive_scope({"analyzed_requirements": list(range(7))}) == {"scope_size": "large", "scope_large": True}


def test_derive_scope_counts_open_gaps_too() -> None:
    # 4 requirements + 4 gaps → score 4 + (4//2) = 6 → large (and 4 gaps flags clarification)
    out = _derive_scope({"analyzed_requirements": list(range(4)), "requirement_gaps": list(range(4))})
    assert out["scope_size"] == "large"
    assert out["scope_large"] is True
    assert out["needs_clarification"] is True


def test_derive_scope_handles_missing_fields() -> None:
    assert _derive_scope({}) == {"scope_size": "small", "scope_large": False}


def test_scope_story_guidance_sizes_decomposition() -> None:
    assert "LARGE" in _scope_story_guidance("large")
    assert "6-12" in _scope_story_guidance("large")
    assert "MEDIUM" in _scope_story_guidance("medium")
    assert "SMALL" in _scope_story_guidance("small")
    assert _scope_story_guidance("") == ""  # no signal → no injected guidance


def test_boardroom_decision_and_scope_are_surfaced() -> None:
    # surfaced so they survive the handover to the planner + implementer
    for key in ("boardroom_decision", "scope_size", "scope_large", "needs_clarification", "clarification_question"):
        assert key in DEFAULT_SURFACE_KEYS


def test_derive_scope_flags_clarification_on_many_gaps() -> None:
    out = _derive_scope({"analyzed_requirements": [1, 2], "requirement_gaps": ["a", "b", "c", "d"]})
    assert out["needs_clarification"] is True
    assert "a" in out["clarification_question"]
    # few gaps → no clarification flag (run proceeds)
    assert "needs_clarification" not in _derive_scope({"analyzed_requirements": [1], "requirement_gaps": ["a"]})


@pytest.mark.asyncio
async def test_clarification_gate_surfaces_question() -> None:
    deps = StageDeps(config=None, scm=None, state_manager=None, llm=None)
    task = DevAITask(intent="x", repo="tesserix/x")
    task.agent_context["clarification_question"] = "Which auth provider?"
    res = await ClarificationGateStage(deps).execute(task)
    assert res.data["clarification_question"] == "Which auth provider?"
    assert res.data["awaiting_clarification"] is True
