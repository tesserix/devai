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

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from devai.sandbox.trace import Invocation

if TYPE_CHECKING:
    from devai.sandbox.invoke import SandboxInvoker
    from devai.sandbox.models import SandboxRecord

logger = logging.getLogger(__name__)

_PREFIX = "devai:sandbox"
_MAX_CASES = 50


class EvalExpect(BaseModel):
    """What has to be true of a turn. Everything unset is simply not checked."""

    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    matches: str = ""
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    max_total_tokens: int | None = None
    max_latency_ms: int | None = None

    model_config = ConfigDict(extra="forbid")


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
    final_text: str = ""
    totals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvalRun:
    id: str
    sandbox_id: str
    agent: str = ""
    results: list[CaseResult] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_ms: int = 0

    @property
    def summary(self) -> dict[str, Any]:
        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        return {
            "cases": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "total_tokens": sum(int(r.totals.get("total_tokens", 0)) for r in self.results),
            "cost_usd": round(sum(float(r.totals.get("cost_usd", 0.0)) for r in self.results), 6),
            "latency_ms": sum(int(r.totals.get("latency_ms", 0)) for r in self.results),
            "duration_ms": self.duration_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sandbox_id": self.sandbox_id,
            "agent": self.agent,
            "created_at": self.created_at,
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> EvalRun:
        known = set(CaseResult.__slots__)
        results = [
            CaseResult(**{k: v for k, v in r.items() if k in known}) for r in body.get("results") or []
        ]
        return cls(
            id=str(body.get("id") or ""),
            sandbox_id=str(body.get("sandbox_id") or ""),
            agent=str(body.get("agent") or ""),
            results=results,
            created_at=str(body.get("created_at") or ""),
            duration_ms=int((body.get("summary") or {}).get("duration_ms") or 0),
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
    def __init__(self, invoker: SandboxInvoker, store: EvalStore) -> None:
        self._invoker = invoker
        self._store = store

    @property
    def store(self) -> EvalStore:
        return self._store

    async def run(self, record: SandboxRecord, cases: list[EvalCase], *, triggered_by: str) -> EvalRun:
        """Every case, in order. Cases run sequentially so a suite cannot burst
        past the provider's rate limit and score itself on 429s."""
        if not cases:
            raise ValueError("a suite needs at least one case")
        if len(cases) > _MAX_CASES:
            raise ValueError(f"a suite is capped at {_MAX_CASES} cases")

        run = EvalRun(id=f"eval-{uuid.uuid4().hex[:12]}", sandbox_id=record.id, agent=record.spec.agent.name)
        started = time.perf_counter()
        for case in cases:
            run.results.append(await self._one(record, case, triggered_by=triggered_by))
        run.duration_ms = int((time.perf_counter() - started) * 1000)

        await self._store.save(run, ttl_seconds=self._ttl(record))
        return run

    async def _one(self, record: SandboxRecord, case: EvalCase, *, triggered_by: str) -> CaseResult:
        try:
            invocation = await self._invoker.invoke(record, message=case.input, triggered_by=triggered_by)
        except Exception as e:  # noqa: BLE001 — one broken case is a red case, not a dead suite
            logger.warning("eval case %s could not run", case.name, exc_info=True)
            return CaseResult(name=case.name, passed=False, failures=[f"could not run: {e}"])

        failures = grade(case.expect, invocation)
        return CaseResult(
            name=case.name,
            passed=not failures,
            failures=failures,
            invocation_id=invocation.id,
            final_text=invocation.final_text,
            totals=invocation.totals,
        )

    def _ttl(self, record: SandboxRecord) -> int:
        remaining = int((record.expires_at - datetime.now(UTC)).total_seconds())
        return max(remaining, 300)


class EvalStore:
    """Runs live beside the traces they judged, with the same TTL."""

    def __init__(self, redis: Any | None) -> None:
        self._redis = redis
        self._local: dict[str, str] = {}
        self._local_index: dict[str, list[str]] = {}

    def _key(self, sandbox_id: str, run_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:evalrun:{run_id}"

    def _index_key(self, sandbox_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:evalruns"

    async def save(self, run: EvalRun, *, ttl_seconds: int) -> None:
        key = self._key(run.sandbox_id, run.id)
        index = self._index_key(run.sandbox_id)
        body = json.dumps(run.to_dict())
        if self._redis is None:
            self._local[key] = body
            self._local_index.setdefault(index, []).insert(0, run.id)
            return
        try:
            await self._redis.set(key, body, ex=ttl_seconds)
            await self._redis.lpush(index, run.id)
            await self._redis.expire(index, ttl_seconds)
        except Exception:  # noqa: BLE001 — a lost result must not fail the suite
            logger.warning("sandbox evals: save failed for %s", run.id, exc_info=True)

    async def get(self, sandbox_id: str, run_id: str) -> EvalRun | None:
        try:
            key = self._key(sandbox_id, run_id)
            body = self._local.get(key) if self._redis is None else await self._redis.get(key)
        except Exception:  # noqa: BLE001
            logger.warning("sandbox evals: read failed for %s", run_id, exc_info=True)
            return None
        return _decode(body)

    async def list_for_sandbox(self, sandbox_id: str, *, limit: int = 20) -> list[EvalRun]:
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
