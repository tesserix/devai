"""Evaluation engine — score a run on objective outcome metrics and persist it.

The deterministic core of the "continuously improving" loop the AI-platform
framework calls for: at end-of-run, score how cleanly the run delivered and
write it to ``agent_evals`` (the analytics quality view), so quality is measured
objectively over time rather than inferred from agent narration.

Generic + config-driven: any blueprint adds ``stage: evaluate`` at the end. The
scorer is a pure function (``score_run``) over the run's own ground truth — PR
produced, gates resolved, stages that succeeded — so it's cheap, LLM-free, and
testable. (An LLM-judge dimension for correctness/groundedness is a noted
follow-up; this is the objective baseline.)
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult

logger = logging.getLogger(__name__)

# Run quality passes when at/above this composite score.
_PASS_BAR = 0.7


def score_run(task: DevAITask) -> tuple[float, bool, dict[str, Any]]:
    """Score a run 0..1 from its own ground truth. Returns (score, passed, breakdown).

    Dimensions (objective, no LLM):
      - delivered   — a pull request was produced (the core deliverable)
      - gates_clean — review / security / tests all resolved (no unblocked verdict)
      - completion  — fraction of attempted stages that completed (vs failed)
    """
    ctx = task.agent_context
    delivered = 1.0 if isinstance(task.pr_number, int) and task.pr_number > 0 else 0.0
    gates_bad = (
        bool(ctx.get("review_changes_requested")) or bool(ctx.get("security_blocked")) or bool(ctx.get("test_failed"))
    )
    gates_clean = 0.0 if gates_bad else 1.0
    completed = len(task.stages_completed)
    failed = len(task.stages_failed)
    completion = completed / max(1, completed + failed)

    score = 0.4 * delivered + 0.3 * gates_clean + 0.3 * completion
    if gates_bad:
        score = min(score, 0.5)  # a run that shipped with an unresolved gate cannot pass
    score = round(score, 3)
    breakdown = {
        "delivered": delivered,
        "gates_clean": gates_clean,
        "completion": round(completion, 3),
        "stages_completed": completed,
        "stages_failed": failed,
        "pr_number": task.pr_number,
        "deploy_status": ctx.get("deploy_status"),
    }
    return score, score >= _PASS_BAR, breakdown


class EvaluateStage(PipelineStage):
    """Score the run and persist it to ``agent_evals`` (the analytics quality
    view). Best-effort: a missing DB / any failure never breaks the run; the
    score is also surfaced in the handover for the report + downstream learning."""

    def __init__(self, deps: StageDeps, *, evaluator: str = "run_quality", name: str = "evaluate") -> None:
        self.deps = deps
        self._evaluator = evaluator
        self._name = name

    def name(self) -> str:
        return self._name

    async def execute(self, task: DevAITask) -> StageResult:
        score, passed, breakdown = score_run(task)
        try:
            from devai.services.database import get_global_db

            db = await get_global_db()
            if db is not None:
                await db.record_eval(
                    run_id=task.id,
                    evaluator=self._evaluator,
                    score=score,
                    passed=passed,
                    triggered_by=getattr(task, "triggered_by", "") or "",
                    detail=breakdown,
                )
        except Exception:  # noqa: BLE001 — capturing an eval never breaks a run
            logger.debug("evaluate: record_eval failed", exc_info=True)

        return StageResult(
            message=f"run quality {score:.2f} ({'pass' if passed else 'below bar'})",
            data={"run_quality_score": score, "run_quality_passed": passed, "run_quality": breakdown},
        )


def evaluate_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Generic ``evaluate`` stage built from blueprint config.

    config: evaluator — the evaluator name recorded (default ``run_quality``).
    """
    return EvaluateStage(deps, evaluator=config.get("evaluator", "run_quality"), name=config.get("name", "evaluate"))


__all__ = ["EvaluateStage", "evaluate_stage", "score_run"]
