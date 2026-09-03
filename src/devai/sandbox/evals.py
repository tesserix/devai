"""Checks — a saved set of inputs and expectations, run against a sandbox.

One chat turn tells you what an agent said once. A suite tells you whether the
definition still behaves after the prompt changed, which is the only question
worth asking before publishing. Cases live on the agent definition itself
(``spec.evals``), so they version and publish with the agent rather than in a
side channel that drifts from it.

Grading reads the trace, never the model: text, chosen tools and budget are all
derived from the same steps the console renders, so a red case can be opened and
seen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from devai.adapters.telemetry import EvaluationMetric, NoopTelemetryAdapter, TelemetryAdapter
from devai.sandbox.trace import Invocation

if TYPE_CHECKING:
    from devai.evaluations.models import JudgeConfig
    from devai.identity import Principal
    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)

_PREFIX = "devai:sandbox"
_GLOBAL_PREFIX = "devai:evaluation"
_DEFAULT_MAX_CASES = 50
OCRStatus = Literal["completed", "partial", "review_required", "rejected"]


def _default_ocr_statuses() -> list[OCRStatus]:
    return ["completed", "partial", "review_required"]


class EvaluationInvoker(Protocol):
    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation: ...


class OCRTableCell(BaseModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    text: str = Field(max_length=10_000)

    model_config = ConfigDict(frozen=True, extra="forbid")


class OCRExpect(BaseModel):
    reference_text: str | None = Field(default=None, max_length=100_000)
    expected_fields: dict[str, Any] = Field(default_factory=dict, max_length=500)
    expected_table_cells: list[OCRTableCell] = Field(default_factory=list, max_length=10_000)
    expected_document_type: str | None = Field(default=None, min_length=1, max_length=100)
    acceptable_statuses: list[OCRStatus] = Field(default_factory=_default_ocr_statuses, min_length=1, max_length=4)
    max_character_error_rate: float | None = Field(default=None, ge=0, le=1)
    max_word_error_rate: float | None = Field(default=None, ge=0, le=1)
    min_field_f1: float | None = Field(default=None, ge=0, le=1)
    min_table_cell_accuracy: float | None = Field(default=None, ge=0, le=1)
    require_citations: bool = True

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvalExpect(BaseModel):
    """What has to be true of a turn. Everything unset is simply not checked."""

    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    matches: str = ""
    exact_output: str | None = None
    json_schema: dict[str, Any] | None = None
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    tool_order: Literal["ordered", "unordered"] = "ordered"
    tool_arguments: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    max_total_tokens: int | None = None
    max_latency_ms: int | None = None
    max_cost_usd: float | None = None
    ocr: OCRExpect | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def arguments_align_with_tools(self) -> EvalExpect:
        if self.tool_arguments and len(self.tool_arguments) != len(self.tools_called):
            raise ValueError("tool_arguments must align one-to-one with tools_called")
        return self


class EvalCase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1)
    expect: EvalExpect = Field(default_factory=EvalExpect)
    human_scores: dict[str, float] = Field(default_factory=dict, max_length=5)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def human_scores_are_normalized(self) -> EvalCase:
        if any(score < 0 or score > 1 for score in self.human_scores.values()):
            raise ValueError("human judge scores must be between 0 and 1")
        return self


@dataclass(slots=True)
class CaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    invocation_id: str = ""
    execution_backend: str = ""
    final_text: str = ""
    totals: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    human_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = asdict(self)
        body["trace_url"] = f"/api/traces/{self.invocation_id}" if self.invocation_id else None
        return body


@dataclass(slots=True)
class EvalRun:
    id: str
    sandbox_id: str
    agent: str = ""
    owner_scope: str = ""
    tenant_id: str = ""
    user_id: str = ""
    dataset_ref: dict[str, str] | None = None
    suite_ref: dict[str, str] | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    judge: dict[str, Any] | None = None
    results: list[CaseResult] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_ms: int = 0
    judge_cost_usd: float = 0.0
    infrastructure_cost_usd: float = 0.0
    # Lives inside the summary jsonb so it round-trips without a schema change.
    status: str = "completed"
    error: str = ""

    @property
    def summary(self) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        agent_cost_usd = round(sum(float(r.totals.get("cost_usd", 0.0)) for r in self.results), 6)
        cost_breakdown = {
            "agent_cost_usd": agent_cost_usd,
            "judge_cost_usd": round(self.judge_cost_usd, 6),
            "infrastructure_cost_usd": round(self.infrastructure_cost_usd, 6),
        }
        case_latencies = sorted(
            int(result.totals.get("wall_clock_ms", result.totals.get("latency_ms", 0))) for result in self.results
        )
        p95_latency_ms = case_latencies[max(0, math.ceil(len(case_latencies) * 0.95) - 1)] if case_latencies else 0
        dimensions: dict[str, dict[str, Any]] = {}
        score_names = dict.fromkeys(name for result in self.results for name in result.scores)
        for name in score_names:
            scores = [result.scores[name] for result in self.results if name in result.scores]
            passed_scores = sum(1 for score in scores if score["passed"])
            dimensions[name] = {
                "average": round(sum(float(score["score"]) for score in scores) / len(scores), 6),
                "passed": passed_scores,
                "failed": len(scores) - passed_scores,
                "pass_rate": round(passed_scores / len(scores), 4),
                "unit": scores[0]["unit"],
            }
        summary = {
            "cases": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "total_tokens": sum(int(r.totals.get("total_tokens", 0)) for r in self.results),
            "cost_usd": round(sum(cost_breakdown.values()), 6),
            "cost_breakdown": cost_breakdown,
            "latency_ms": sum(int(r.totals.get("latency_ms", 0)) for r in self.results),
            "p95_latency_ms": p95_latency_ms,
            "duration_ms": self.duration_ms,
            "dimensions": dimensions,
            "status": self.status,
        }
        if self.error:
            summary["error"] = self.error
        calibration = self._calibration()
        if calibration is not None:
            summary["calibration"] = calibration
        return summary

    def _calibration(self) -> dict[str, Any] | None:
        pairs: list[tuple[float, float, float]] = []
        for result in self.results:
            judge = result.scores.get("llm_judge", {}).get("detail", {})
            threshold = float(judge.get("pass_threshold", 0.7))
            judged_dimensions = judge.get("dimensions", {})
            for dimension, human_score in result.human_scores.items():
                judged = judged_dimensions.get(dimension)
                if isinstance(judged, dict) and "score" in judged:
                    pairs.append((float(human_score), float(judged["score"]), threshold))
        if not pairs:
            return None
        agreements = sum(1 for human, judged, threshold in pairs if (human >= threshold) == (judged >= threshold))
        return {
            "labelled_scores": len(pairs),
            "threshold_agreement": round(agreements / len(pairs), 4),
            "mean_absolute_error": round(sum(abs(human - judged) for human, judged, _ in pairs) / len(pairs), 6),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sandbox_id": self.sandbox_id,
            "agent": self.agent,
            "dataset": self.dataset_ref,
            "suite": self.suite_ref,
            "configuration": self.configuration,
            "judge": self.judge,
            "created_at": self.created_at,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            **self.to_dict(),
            "owner_scope": self.owner_scope,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> EvalRun:
        known = set(CaseResult.__slots__)
        results = [CaseResult(**{k: v for k, v in r.items() if k in known}) for r in body.get("results") or []]
        summary = body.get("summary") or {}
        cost_breakdown = summary.get("cost_breakdown") or {}
        return cls(
            id=str(body.get("id") or ""),
            sandbox_id=str(body.get("sandbox_id") or ""),
            agent=str(body.get("agent") or ""),
            owner_scope=str(body.get("owner_scope") or ""),
            tenant_id=str(body.get("tenant_id") or ""),
            user_id=str(body.get("user_id") or ""),
            dataset_ref=body.get("dataset"),
            suite_ref=body.get("suite"),
            configuration=body.get("configuration") or {},
            judge=body.get("judge"),
            results=results,
            created_at=str(body.get("created_at") or ""),
            status=str(body.get("status") or summary.get("status") or "completed"),
            error=str(summary.get("error") or ""),
            duration_ms=int(summary.get("duration_ms") or 0),
            judge_cost_usd=float(cost_breakdown.get("judge_cost_usd") or 0.0),
            infrastructure_cost_usd=float(cost_breakdown.get("infrastructure_cost_usd") or 0.0),
        )


def grade(expect: EvalExpect, invocation: Invocation) -> list[str]:
    """Every expectation this turn broke. Empty means the case passed."""
    if not invocation.ok:
        return [f"run failed: {invocation.error or 'unknown error'}"]

    failures: list[str] = []
    text = (invocation.final_text or "").lower()
    for phrase in expect.contains:
        if phrase.lower() not in text:
            failures.append(f"missing expected text: {phrase!r}")
    for phrase in expect.not_contains:
        if phrase.lower() in text:
            failures.append(f"found forbidden text: {phrase!r}")
    if expect.matches:
        try:
            if not re.search(expect.matches, invocation.final_text or "", re.IGNORECASE):
                failures.append(f"no match for regex {expect.matches!r}")
        except re.error as e:
            failures.append(f"invalid regex {expect.matches!r}: {e}")

    called = {s.name for s in invocation.steps if s.kind == "tool"}
    failures.extend(f"tool not called: {t!r}" for t in expect.tools_called if t not in called)
    failures.extend(f"tool should not have been called: {t!r}" for t in expect.tools_not_called if t in called)

    totals = invocation.totals
    if expect.max_total_tokens is not None and totals["total_tokens"] > expect.max_total_tokens:
        failures.append(f"used {totals['total_tokens']} tokens, budget {expect.max_total_tokens}")
    if expect.max_latency_ms is not None and totals["latency_ms"] > expect.max_latency_ms:
        failures.append(f"took {totals['latency_ms']} ms, budget {expect.max_latency_ms} ms")
    return failures


class EvalRunner:
    def __init__(
        self,
        invoker: EvaluationInvoker,
        store: EvalStore,
        *,
        max_cases: int = _DEFAULT_MAX_CASES,
        max_concurrency: int = 4,
        judge_factory: Any | None = None,
        telemetry: TelemetryAdapter | None = None,
    ) -> None:
        self._invoker = invoker
        self._store = store
        self._max_cases = max(1, max_cases)
        self._max_concurrency = max(1, max_concurrency)
        self._judge_factory = judge_factory
        self._tasks: set[asyncio.Task[None]] = set()
        self._telemetry = telemetry or NoopTelemetryAdapter()

    @property
    def store(self) -> EvalStore:
        return self._store

    async def run(
        self,
        record: SandboxRecord,
        cases: list[EvalCase],
        *,
        triggered_by: str,
        owner_scope: str = "",
        tenant_id: str = "",
        user_id: str = "",
        dataset_ref: dict[str, str] | None = None,
        suite_ref: dict[str, str] | None = None,
        scorers: list[str] | None = None,
        principal: Principal | None = None,
        judge_config: JudgeConfig | None = None,
        run_id: str | None = None,
    ) -> EvalRun:
        """Run every case with bounded fan-out and preserve dataset order."""
        run, created, judge_scorers, deterministic_scorers = await self._admit(
            record,
            cases,
            owner_scope=owner_scope,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_ref=dataset_ref,
            suite_ref=suite_ref,
            scorers=scorers,
            judge_config=judge_config,
            run_id=run_id,
        )
        if not created:
            return run
        await self._execute(
            run,
            record,
            cases,
            judge_scorers,
            deterministic_scorers,
            triggered_by=triggered_by,
            principal=principal,
            judge_config=judge_config,
        )
        return run

    async def start(
        self,
        record: SandboxRecord,
        cases: list[EvalCase],
        *,
        triggered_by: str,
        owner_scope: str = "",
        tenant_id: str = "",
        user_id: str = "",
        dataset_ref: dict[str, str] | None = None,
        suite_ref: dict[str, str] | None = None,
        scorers: list[str] | None = None,
        principal: Principal | None = None,
        judge_config: JudgeConfig | None = None,
        run_id: str | None = None,
    ) -> EvalRun:
        """Persist a running record and finish in the background.

        A suite on the kubernetes_job backend takes minutes — longer than any
        proxy in front of the API holds a request open — so the caller gets the
        durable id immediately and polls ``status`` until it is terminal.
        """
        run, created, judge_scorers, deterministic_scorers = await self._admit(
            record,
            cases,
            owner_scope=owner_scope,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_ref=dataset_ref,
            suite_ref=suite_ref,
            scorers=scorers,
            judge_config=judge_config,
            run_id=run_id,
        )
        if not created:
            return run  # idempotent replay of an already-started run
        await self._store.save(run, ttl_seconds=self._ttl(record))

        async def finish() -> None:
            try:
                await self._execute(
                    run,
                    record,
                    cases,
                    judge_scorers,
                    deterministic_scorers,
                    triggered_by=triggered_by,
                    principal=principal,
                    judge_config=judge_config,
                )
            except Exception as exc:  # noqa: BLE001 — a lost suite must still reach a terminal state
                logger.exception("eval run %s failed in the background", run.id)
                run.status = "failed"
                run.error = str(exc)[:500]
                await self._store.save(run, ttl_seconds=self._ttl(record))

        task = asyncio.create_task(finish())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def _admit(
        self,
        record: SandboxRecord,
        cases: list[EvalCase],
        *,
        owner_scope: str,
        tenant_id: str,
        user_id: str,
        dataset_ref: dict[str, str] | None,
        suite_ref: dict[str, str] | None,
        scorers: list[str] | None,
        judge_config: JudgeConfig | None,
        run_id: str | None,
    ) -> tuple[EvalRun, bool, list[Any], list[Any]]:
        from devai.evaluations.scorers import bind

        if not cases:
            raise ValueError("a suite needs at least one case")
        if len(cases) > self._max_cases:
            raise ValueError(f"tenant dataset quota is {self._max_cases} cases per eval run")
        bound_scorers = bind(scorers or [])
        judge_scorers = [scorer for scorer in bound_scorers if scorer.name == "llm_judge"]
        deterministic_scorers = [scorer for scorer in bound_scorers if scorer.name != "llm_judge"]
        if bool(judge_scorers) != (judge_config is not None):
            raise ValueError("llm_judge scorer requires exactly one pinned judge configuration")

        if run_id:
            existing = await self._store.get_by_id(owner_scope, run_id)
            if existing is not None:
                if existing.sandbox_id != record.id:
                    raise ValueError("idempotent evaluation request resolved to a different sandbox")
                return existing, False, judge_scorers, deterministic_scorers
        agent = record.spec.agent
        if agent is None:
            raise ValueError(f"sandbox {record.id} has no agent")

        run = EvalRun(
            id=run_id or f"eval-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=agent.name,
            owner_scope=owner_scope,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_ref=dataset_ref,
            suite_ref=suite_ref,
            configuration=record.spec.model_dump(mode="json"),
            status="running",
        )
        return run, True, judge_scorers, deterministic_scorers

    async def _execute(
        self,
        run: EvalRun,
        record: SandboxRecord,
        cases: list[EvalCase],
        judge_scorers: list[Any],
        deterministic_scorers: list[Any],
        *,
        triggered_by: str,
        principal: Principal | None,
        judge_config: JudgeConfig | None,
    ) -> None:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self._max_concurrency)
        results: list[CaseResult | None] = [None] * len(cases)
        invocations: list[Invocation | None] = [None] * len(cases)

        async def run_case(index: int, case: EvalCase) -> None:
            async with semaphore:
                results[index], invocations[index] = await self._one(
                    record,
                    case,
                    triggered_by=triggered_by,
                    scorers=deterministic_scorers,
                )

        async with asyncio.TaskGroup() as group:
            for index, case in enumerate(cases):
                group.create_task(run_case(index, case))
        run.results = [result for result in results if result is not None]
        if judge_scorers and judge_config is not None:
            await self._judge(
                run,
                record,
                cases,
                invocations,
                judge_scorers,
                principal=principal,
                judge_config=judge_config,
            )
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        run.status = "completed"
        run.error = ""

        await self._store.save(run, ttl_seconds=self._ttl(record))
        summary = run.summary
        suite = run.suite_ref or run.dataset_ref or {}
        try:
            await self._telemetry.record_evaluation(
                EvaluationMetric(
                    run_id=run.id,
                    agent=run.agent,
                    suite=f"{suite.get('name', 'inline')}@{suite.get('version', 'draft')}",
                    pass_rate=float(summary["pass_rate"]),
                    case_count=int(summary["cases"]),
                    cost_usd=float(summary["cost_usd"]),
                    total_tokens=int(summary["total_tokens"]),
                    p95_latency_ms=float(summary["p95_latency_ms"]),
                    dimensions={name: float(values["average"]) for name, values in summary["dimensions"].items()},
                    failing_case_ids=[result.name for result in run.results if not result.passed],
                )
            )
        except Exception:  # noqa: BLE001 — telemetry cannot invalidate a saved evaluation
            logger.warning("evaluation telemetry failed for %s", run.id, exc_info=True)

    async def _one(
        self,
        record: SandboxRecord,
        case: EvalCase,
        *,
        triggered_by: str,
        scorers: list[Any],
    ) -> tuple[CaseResult, Invocation | None]:
        try:
            invocation = await self._invoker.invoke(record, message=case.input, triggered_by=triggered_by)
        except Exception as e:  # noqa: BLE001 — one broken case is a red case, not a dead suite
            logger.warning("eval case %s could not run", case.name, exc_info=True)
            result = CaseResult(
                name=case.name,
                passed=False,
                failures=[f"could not run: {e}"],
                scores=await self._score(case, None, scorers),
                human_scores=case.human_scores,
            )
            return result, None

        scores = await self._score(case, invocation, scorers)
        if scores:
            failures = [
                f"{name}: {score['detail'].get('failure') or score['detail'].get('error', 'failed')}"
                for name, score in scores.items()
                if not score["passed"]
            ]
        else:
            failures = grade(case.expect, invocation)
        result = CaseResult(
            name=case.name,
            passed=not failures,
            failures=failures,
            invocation_id=invocation.id,
            execution_backend=invocation.execution_backend,
            final_text=invocation.final_text,
            totals=invocation.totals,
            scores=scores,
            human_scores=case.human_scores,
        )
        return result, invocation

    @staticmethod
    async def _score(
        case: EvalCase,
        invocation: Invocation | None,
        scorers: list[Any],
        *,
        judge: Any = None,
    ) -> dict[str, dict[str, Any]]:
        import inspect

        from devai.evaluations.scorers import ScorerContext, ScorerResult

        results: dict[str, dict[str, Any]] = {}
        context = ScorerContext(invocation=invocation, expect=case.expect, judge=judge)
        for scorer in scorers:
            try:
                result = scorer.score(context)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001 — one broken scorer is a failed dimension
                logger.warning("eval scorer %s failed", scorer.name, exc_info=True)
                result = ScorerResult(
                    name=scorer.name,
                    score=0.0,
                    passed=False,
                    detail={"error": "scorer failed"},
                )
            results[scorer.name] = result.to_dict()
        return results

    async def _judge(
        self,
        run: EvalRun,
        record: SandboxRecord,
        cases: list[EvalCase],
        invocations: list[Invocation | None],
        scorers: list[Any],
        *,
        principal: Principal | None,
        judge_config: JudgeConfig,
    ) -> None:
        from devai.evaluations.judge import JudgeBudget

        run.judge = judge_config.model_dump(mode="json")
        agent_cost = sum(float(result.totals.get("cost_usd", 0.0)) for result in run.results)
        budget = JudgeBudget(remaining_usd=max(0.0, record.spec.limits.max_cost_usd - agent_cost))
        try:
            if self._judge_factory is None or principal is None:
                raise ValueError("judge runtime unavailable")
            judge, effective_config = await self._judge_factory.create(
                principal=principal,
                config=judge_config,
                budget=budget,
                metadata={
                    "tenant_id": run.tenant_id,
                    "user_id": run.user_id,
                    "run_id": run.id,
                    "sandbox_id": run.sandbox_id,
                },
            )
            run.judge = effective_config.model_dump(mode="json")
        except Exception:  # noqa: BLE001 — judge availability must not discard deterministic results
            logger.warning("evaluation judge could not be resolved", exc_info=True)
            self._mark_judge_unavailable(run, "judge runtime unavailable")
            return

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def score_case(index: int) -> None:
            async with semaphore:
                scores = await self._score(cases[index], invocations[index], scorers, judge=judge)
                run.results[index].scores.update(scores)
                self._refresh_result(run.results[index])

        async with asyncio.TaskGroup() as group:
            for index in range(len(cases)):
                group.create_task(score_case(index))
        run.judge_cost_usd = budget.spent_usd

    @staticmethod
    def _mark_judge_unavailable(run: EvalRun, error: str) -> None:
        from devai.evaluations.scorers import ScorerResult

        score = ScorerResult(name="llm_judge", score=0.0, passed=False, detail={"error": error}).to_dict()
        for result in run.results:
            result.scores["llm_judge"] = score
            EvalRunner._refresh_result(result)

    @staticmethod
    def _refresh_result(result: CaseResult) -> None:
        result.failures = [
            f"{name}: {score['detail'].get('failure') or score['detail'].get('error', 'failed')}"
            for name, score in result.scores.items()
            if not score["passed"]
        ]
        result.passed = not result.failures

    def _ttl(self, record: SandboxRecord) -> int:
        remaining = int((record.expires_at - datetime.now(UTC)).total_seconds())
        return max(remaining, 300)


class EvalRunDatabase(Protocol):
    async def save_eval_run(self, run: dict[str, Any]) -> None: ...

    async def get_eval_run(self, owner_scope: str, sandbox_id: str, run_id: str) -> dict[str, Any] | None: ...

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None: ...

    async def list_eval_runs(
        self,
        owner_scope: str,
        sandbox_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class EvalStore:
    """Durable in Postgres, with an ephemeral fallback for isolated tests."""

    def __init__(self, redis: Any | None, *, database: EvalRunDatabase | None = None) -> None:
        self._redis = redis
        self._database = database
        self._local: dict[str, str] = {}
        self._local_index: dict[str, list[str]] = {}

    def _key(self, sandbox_id: str, run_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:evalrun:{run_id}"

    def _index_key(self, sandbox_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:evalruns"

    def _global_key(self, run_id: str) -> str:
        return f"{_GLOBAL_PREFIX}:{run_id}"

    async def save(self, run: EvalRun, *, ttl_seconds: int) -> None:
        if self._database is not None:
            await self._database.save_eval_run(run.to_storage_dict())
            return
        key = self._key(run.sandbox_id, run.id)
        index = self._index_key(run.sandbox_id)
        global_key = self._global_key(run.id)
        body = json.dumps(run.to_storage_dict())
        locator = json.dumps({"sandbox_id": run.sandbox_id, "owner_scope": run.owner_scope})
        if self._redis is None:
            self._local[key] = body
            self._local[global_key] = locator
            bucket = self._local_index.setdefault(index, [])
            if run.id not in bucket:  # a run saves twice: once running, once terminal
                bucket.insert(0, run.id)
            return
        try:
            await self._redis.set(key, body, ex=ttl_seconds)
            await self._redis.set(global_key, locator, ex=ttl_seconds)
            await self._redis.lrem(index, 0, run.id)
            await self._redis.lpush(index, run.id)
            await self._redis.expire(index, ttl_seconds)
        except Exception:  # noqa: BLE001 — a lost result must not fail the suite
            logger.warning("sandbox evals: save failed for %s", run.id, exc_info=True)

    async def get(self, sandbox_id: str, run_id: str, *, owner_scope: str = "") -> EvalRun | None:
        if self._database is not None:
            stored = await self._database.get_eval_run(owner_scope, sandbox_id, run_id)
            return EvalRun.from_dict(stored) if stored else None
        try:
            key = self._key(sandbox_id, run_id)
            body = self._local.get(key) if self._redis is None else await self._redis.get(key)
        except Exception:  # noqa: BLE001
            logger.warning("sandbox evals: read failed for %s", run_id, exc_info=True)
            return None
        return _decode(body)

    async def get_by_id(self, owner_scope: str, run_id: str) -> EvalRun | None:
        if self._database is not None:
            stored = await self._database.get_eval_run_by_id(owner_scope, run_id)
            return EvalRun.from_dict(stored) if stored else None
        try:
            key = self._global_key(run_id)
            locator = self._local.get(key) if self._redis is None else await self._redis.get(key)
            if isinstance(locator, bytes):
                locator = locator.decode()
            metadata = json.loads(locator) if locator else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("sandbox evals: global lookup failed for %s", run_id, exc_info=True)
            return None
        if metadata.get("owner_scope") != owner_scope:
            return None
        return await self.get(str(metadata.get("sandbox_id") or ""), run_id, owner_scope=owner_scope)

    async def list_for_sandbox(
        self,
        sandbox_id: str,
        *,
        limit: int = 20,
        owner_scope: str = "",
    ) -> list[EvalRun]:
        if self._database is not None:
            rows = await self._database.list_eval_runs(owner_scope, sandbox_id, limit=limit)
            return [EvalRun.from_dict(row) for row in rows]
        index = self._index_key(sandbox_id)
        try:
            if self._redis is None:
                ids = self._local_index.get(index, [])[:limit]
            else:
                ids = [str(i) for i in await self._redis.lrange(index, 0, limit - 1)]
        except Exception:  # noqa: BLE001
            logger.warning("sandbox evals: index read failed for %s", sandbox_id, exc_info=True)
            return []
        found = [await self.get(sandbox_id, i) for i in ids]
        return [run for run in found if run is not None]


def _decode(body: Any) -> EvalRun | None:
    if not body:
        return None
    if isinstance(body, bytes):
        body = body.decode()
    try:
        return EvalRun.from_dict(json.loads(body))
    except Exception:  # noqa: BLE001 — a corrupt record reads as absent
        logger.warning("sandbox evals: corrupt record skipped", exc_info=True)
        return None


__all__ = ["CaseResult", "EvalCase", "EvalExpect", "EvalRun", "EvalRunner", "EvalStore", "grade"]
