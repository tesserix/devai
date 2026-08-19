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

from devai.sandbox.trace import Invocation

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)

_PREFIX = "devai:sandbox"
_GLOBAL_PREFIX = "devai:evaluation"
_DEFAULT_MAX_CASES = 50


class EvaluationInvoker(Protocol):
    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation: ...


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

    model_config = ConfigDict(extra="forbid")


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
    results: list[CaseResult] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_ms: int = 0
    judge_cost_usd: float = 0.0
    infrastructure_cost_usd: float = 0.0

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
        return {
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
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sandbox_id": self.sandbox_id,
            "agent": self.agent,
            "dataset": self.dataset_ref,
            "suite": self.suite_ref,
            "created_at": self.created_at,
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
            results=results,
            created_at=str(body.get("created_at") or ""),
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
    ) -> None:
        self._invoker = invoker
        self._store = store
        self._max_cases = max(1, max_cases)
        self._max_concurrency = max(1, max_concurrency)

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
    ) -> EvalRun:
        """Run every case with bounded fan-out and preserve dataset order."""
        from devai.evaluations.scorers import bind

        if not cases:
            raise ValueError("a suite needs at least one case")
        if len(cases) > self._max_cases:
            raise ValueError(f"tenant dataset quota is {self._max_cases} cases per eval run")
        bound_scorers = bind(scorers or [])

        run = EvalRun(
            id=f"eval-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=record.spec.agent.name,
            owner_scope=owner_scope,
            tenant_id=tenant_id,
            user_id=user_id,
            dataset_ref=dataset_ref,
            suite_ref=suite_ref,
        )
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self._max_concurrency)
        results: list[CaseResult | None] = [None] * len(cases)

        async def run_case(index: int, case: EvalCase) -> None:
            async with semaphore:
                results[index] = await self._one(
                    record,
                    case,
                    triggered_by=triggered_by,
                    scorers=bound_scorers,
                )

        async with asyncio.TaskGroup() as group:
            for index, case in enumerate(cases):
                group.create_task(run_case(index, case))
        run.results = [result for result in results if result is not None]
        run.duration_ms = int((time.perf_counter() - started) * 1000)

        await self._store.save(run, ttl_seconds=self._ttl(record))
        return run

    async def _one(
        self,
        record: SandboxRecord,
        case: EvalCase,
        *,
        triggered_by: str,
        scorers: list[Any],
    ) -> CaseResult:
        try:
            invocation = await self._invoker.invoke(record, message=case.input, triggered_by=triggered_by)
        except Exception as e:  # noqa: BLE001 — one broken case is a red case, not a dead suite
            logger.warning("eval case %s could not run", case.name, exc_info=True)
            return CaseResult(
                name=case.name,
                passed=False,
                failures=[f"could not run: {e}"],
                scores=self._score(case, None, scorers),
            )

        scores = self._score(case, invocation, scorers)
        if scores:
            failures = [
                f"{name}: {score['detail'].get('failure') or score['detail'].get('error', 'failed')}"
                for name, score in scores.items()
                if not score["passed"]
            ]
        else:
            failures = grade(case.expect, invocation)
        return CaseResult(
            name=case.name,
            passed=not failures,
            failures=failures,
            invocation_id=invocation.id,
            execution_backend=invocation.execution_backend,
            final_text=invocation.final_text,
            totals=invocation.totals,
            scores=scores,
        )

    @staticmethod
    def _score(case: EvalCase, invocation: Invocation | None, scorers: list[Any]) -> dict[str, dict[str, Any]]:
        from devai.evaluations.scorers import ScorerContext, ScorerResult

        results: dict[str, dict[str, Any]] = {}
        context = ScorerContext(invocation=invocation, expect=case.expect)
        for scorer in scorers:
            try:
                result = scorer.score(context)
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
            self._local_index.setdefault(index, []).insert(0, run.id)
            return
        try:
            await self._redis.set(key, body, ex=ttl_seconds)
            await self._redis.set(global_key, locator, ex=ttl_seconds)
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
