from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


@dataclass(slots=True, frozen=True)
class ScorerResult:
    name: str
    score: float
    passed: bool
    unit: str = "ratio"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "passed": self.passed,
            "unit": self.unit,
            "detail": self.detail,
        }


@dataclass(slots=True, frozen=True)
class ScorerContext:
    invocation: Any = None
    expect: Any = None
    task: Any = None


ScoreFunction = Callable[[ScorerContext], ScorerResult]


@dataclass(slots=True, frozen=True)
class BoundScorer:
    name: str
    score: ScoreFunction


class ScorerRegistry:
    def __init__(self) -> None:
        self._scorers: dict[str, ScoreFunction] = {}

    def register(self, name: str, scorer: ScoreFunction, *, overwrite: bool = False) -> None:
        if name in self._scorers and not overwrite:
            return
        self._scorers[name] = scorer

    def known(self) -> list[str]:
        return sorted(self._scorers)

    def bind(self, names: list[str]) -> list[BoundScorer]:
        bound: list[BoundScorer] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            scorer = self._scorers.get(name)
            if scorer is None:
                raise ValueError(f"unknown scorer {name!r}")
            bound.append(BoundScorer(name=name, score=scorer))
        return bound


def _failed(name: str, error: str, *, unit: str = "ratio") -> ScorerResult:
    return ScorerResult(name=name, score=0.0, passed=False, unit=unit, detail={"error": error})


def _invocation(context: ScorerContext, name: str) -> Any | ScorerResult:
    invocation = context.invocation
    if invocation is None:
        return _failed(name, "invocation unavailable")
    if not invocation.ok:
        return _failed(name, "invocation failed")
    return invocation


def exact_match(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "exact_match")
    if isinstance(invocation, ScorerResult):
        return invocation
    expected = getattr(context.expect, "exact_output", None)
    if expected is None:
        return _failed("exact_match", "exact output expectation is not configured")
    passed = invocation.final_text == expected
    return ScorerResult(
        name="exact_match",
        score=1.0 if passed else 0.0,
        passed=passed,
        detail={} if passed else {"error": "output did not exactly match"},
    )


def regex(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "regex")
    if isinstance(invocation, ScorerResult):
        return invocation
    pattern = str(getattr(context.expect, "matches", "") or "")
    if not pattern:
        return _failed("regex", "regex expectation is not configured")
    try:
        passed = re.search(pattern, invocation.final_text or "", re.IGNORECASE) is not None
    except re.error:
        return _failed("regex", "regex expectation is invalid")
    return ScorerResult(
        name="regex",
        score=1.0 if passed else 0.0,
        passed=passed,
        detail={} if passed else {"error": "output did not match regex"},
    )


def json_schema(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "json_schema")
    if isinstance(invocation, ScorerResult):
        return invocation
    schema = getattr(context.expect, "json_schema", None)
    if schema is None:
        return _failed("json_schema", "JSON schema expectation is not configured")
    try:
        body = json.loads(invocation.final_text or "")
    except (TypeError, json.JSONDecodeError):
        return _failed("json_schema", "output is not valid JSON")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(body)
    except SchemaError:
        return _failed("json_schema", "JSON schema expectation is invalid")
    except ValidationError:
        return _failed("json_schema", "output does not match JSON schema")
    return ScorerResult(name="json_schema", score=1.0, passed=True)


def expected_tool_call(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "expected_tool_call")
    if isinstance(invocation, ScorerResult):
        return invocation
    expected = list(getattr(context.expect, "tools_called", None) or [])
    forbidden = list(getattr(context.expect, "tools_not_called", None) or [])
    if not expected and not forbidden:
        return _failed("expected_tool_call", "tool-call expectation is not configured")
    called = {step.name for step in invocation.steps if step.kind == "tool"}
    missing = [name for name in expected if name not in called]
    unexpected = [name for name in forbidden if name in called]
    passed = not missing and not unexpected
    detail: dict[str, Any] = {}
    if missing:
        detail["missing"] = missing
    if unexpected:
        detail["forbidden"] = unexpected
    if not passed:
        detail["error"] = "tool-call expectation failed"
    return ScorerResult(name="expected_tool_call", score=1.0 if passed else 0.0, passed=passed, detail=detail)


def tool_trajectory(context: ScorerContext) -> ScorerResult:
    """Compatibility name for suites created before `expected_tool_call`."""
    result = expected_tool_call(context)
    return ScorerResult(
        name="tool_trajectory",
        score=result.score,
        passed=result.passed,
        unit=result.unit,
        detail=result.detail,
    )


def task_completion(context: ScorerContext) -> ScorerResult:
    invocation = context.invocation
    passed = bool(invocation is not None and invocation.ok)
    return ScorerResult(
        name="task_completion",
        score=1.0 if passed else 0.0,
        passed=passed,
        detail={} if passed else {"error": "invocation did not complete"},
    )


def latency(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "latency")
    if isinstance(invocation, ScorerResult):
        return ScorerResult(**{**invocation.to_dict(), "unit": "milliseconds"})
    measured = float(invocation.totals["wall_clock_ms"] or invocation.totals["latency_ms"])
    limit = getattr(context.expect, "max_latency_ms", None)
    passed = limit is None or measured <= limit
    return ScorerResult(
        name="latency",
        score=measured,
        passed=passed,
        unit="milliseconds",
        detail={} if passed else {"error": "latency budget exceeded", "limit": limit},
    )


def tokens(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "tokens")
    if isinstance(invocation, ScorerResult):
        return ScorerResult(**{**invocation.to_dict(), "unit": "tokens"})
    measured = float(invocation.totals["total_tokens"])
    limit = getattr(context.expect, "max_total_tokens", None)
    passed = limit is None or measured <= limit
    return ScorerResult(
        name="tokens",
        score=measured,
        passed=passed,
        unit="tokens",
        detail={} if passed else {"error": "token budget exceeded", "limit": limit},
    )


def cost(context: ScorerContext) -> ScorerResult:
    invocation = _invocation(context, "cost")
    if isinstance(invocation, ScorerResult):
        return ScorerResult(**{**invocation.to_dict(), "unit": "usd"})
    measured = float(invocation.totals["cost_usd"])
    limit = getattr(context.expect, "max_cost_usd", None)
    passed = limit is None or measured <= limit
    return ScorerResult(
        name="cost",
        score=measured,
        passed=passed,
        unit="usd",
        detail={} if passed else {"error": "cost budget exceeded", "limit": limit},
    )


def run_quality(context: ScorerContext) -> ScorerResult:
    task = context.task
    if task is None:
        return _failed("run_quality", "pipeline run unavailable")
    run_context = getattr(task, "agent_context", None)
    if run_context is None:
        run_context = getattr(task, "context", {})
    delivered = 1.0 if isinstance(getattr(task, "pr_number", None), int) and task.pr_number > 0 else 0.0
    gates_bad = (
        bool(run_context.get("review_changes_requested"))
        or bool(run_context.get("security_blocked"))
        or bool(run_context.get("test_failed"))
    )
    gates_clean = 0.0 if gates_bad else 1.0
    completed = len(task.stages_completed)
    failed = len(task.stages_failed)
    completion = completed / max(1, completed + failed)
    score = 0.4 * delivered + 0.3 * gates_clean + 0.3 * completion
    if gates_bad:
        score = min(score, 0.5)
    score = round(score, 3)
    detail = {
        "delivered": delivered,
        "gates_clean": gates_clean,
        "completion": round(completion, 3),
        "stages_completed": completed,
        "stages_failed": failed,
        "pr_number": task.pr_number,
        "deploy_status": run_context.get("deploy_status"),
    }
    return ScorerResult(name="run_quality", score=score, passed=score >= 0.7, detail=detail)


_REGISTRY = ScorerRegistry()


def register(name: str, scorer: ScoreFunction, *, overwrite: bool = False) -> None:
    _REGISTRY.register(name, scorer, overwrite=overwrite)


def known() -> list[str]:
    return _REGISTRY.known()


def bind(names: list[str]) -> list[BoundScorer]:
    return _REGISTRY.bind(names)


for _name, _scorer in {
    "exact_match": exact_match,
    "regex": regex,
    "json_schema": json_schema,
    "expected_tool_call": expected_tool_call,
    "tool_trajectory": tool_trajectory,
    "task_completion": task_completion,
    "latency": latency,
    "tokens": tokens,
    "cost": cost,
    "run_quality": run_quality,
}.items():
    register(_name, _scorer)


__all__ = [
    "BoundScorer",
    "ScorerContext",
    "ScorerRegistry",
    "ScorerResult",
    "bind",
    "known",
    "register",
    "run_quality",
]
