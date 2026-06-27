"""Phase 6 — evaluation engine.

Objective, LLM-free run scoring (delivered / gates-clean / completion) persisted
to agent_evals, so quality is measured over time instead of inferred from agent
narration."""

from __future__ import annotations

import pytest

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.evals import EvaluateStage, score_run
from devai.pipeline.types import DevAITask


def _task(*, pr=None, completed=None, failed=None, **flags) -> DevAITask:  # noqa: ANN003
    t = DevAITask(intent="x", repo="tesserix/x")
    t.pr_number = pr
    t.stages_completed = completed or []
    t.stages_failed = failed or []
    for k in ("review_changes_requested", "security_blocked", "test_failed"):
        if flags.get(k):
            t.agent_context[k] = True
    return t


def test_clean_delivered_run_scores_top() -> None:
    score, passed, b = score_run(_task(pr=7, completed=["a", "b", "c"]))
    assert score == 1.0 and passed is True
    assert b["delivered"] == 1.0 and b["gates_clean"] == 1.0 and b["completion"] == 1.0


def test_unresolved_gate_cannot_pass() -> None:
    score, passed, _ = score_run(_task(pr=7, completed=["a", "b"], security_blocked=True))
    assert score <= 0.5 and passed is False  # capped — shipped with a blocking verdict


def test_no_pr_and_failures_scores_low() -> None:
    score, passed, b = score_run(_task(pr=None, completed=["a"], failed=["b", "c"]))
    assert b["delivered"] == 0.0
    assert score < 0.7 and passed is False


@pytest.mark.asyncio
async def test_evaluate_stage_surfaces_score() -> None:
    deps = StageDeps(config=None, scm=None, state_manager=None, llm=None)
    res = await EvaluateStage(deps).execute(_task(pr=7, completed=["a", "b", "c"]))
    assert res.data["run_quality_score"] == 1.0
    assert res.data["run_quality_passed"] is True
    assert "completion" in res.data["run_quality"]
