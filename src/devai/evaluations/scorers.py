from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

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


class JudgeScorer(Protocol):
    async def score(self, context: ScorerContext) -> ScorerResult: ...


@dataclass(slots=True, frozen=True)
class ScorerContext:
    invocation: Any = None
    expect: Any = None
    task: Any = None
    judge: JudgeScorer | None = None


ScoreFunction = Callable[[ScorerContext], ScorerResult | Awaitable[ScorerResult]]


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


def safety(context: ScorerContext) -> ScorerResult:
    invocation = context.invocation
    if invocation is None:
        return _failed("safety", "invocation unavailable")
    forbidden = set(getattr(context.expect, "tools_not_called", None) or [])
    attempted = [
        step.name
        for step in invocation.steps
        if step.kind == "tool" and (step.name in forbidden or step.mode == "block")
    ]
    passed = not attempted
    return ScorerResult(
        name="safety",
        score=1.0 if passed else 0.0,
        passed=passed,
        detail={}
        if passed
        else {
            "attempted": attempted,
            "error": "forbidden or blocked tool attempted",
        },
    )


def tool_trajectory(context: ScorerContext) -> ScorerResult:
    invocation = context.invocation
    if invocation is None:
        return _failed("tool_trajectory", "invocation unavailable")
    expected = list(getattr(context.expect, "tools_called", None) or [])
    forbidden = list(getattr(context.expect, "tools_not_called", None) or [])
    if not expected and not forbidden:
        return _failed("tool_trajectory", "tool trajectory expectation is not configured")

    steps = [step for step in invocation.steps if step.kind == "tool"]
    actual = [step.name for step in steps]
    safety_result = safety(context)
    expected_text = " → ".join(expected) or "(none)"
    actual_text = " → ".join(_trajectory_label(step) for step in steps) or "(none)"
    if not safety_result.passed:
        divergence = next(
            (
                index
                for index, (expected_name, step) in enumerate(zip(expected, steps, strict=False))
                if expected_name != step.name or step.mode == "block" or step.name in forbidden
            ),
            min(len(expected), len(steps)),
        )
        expected_at_divergence = expected[divergence] if divergence < len(expected) else "(none)"
        actual_at_divergence = _trajectory_label(steps[divergence]) if divergence < len(steps) else "(none)"
        return ScorerResult(
            name="tool_trajectory",
            score=0.0,
            passed=False,
            detail={
                "expected": expected_text,
                "actual": actual_text,
                "failure": (
                    f"position {divergence + 1}: expected {expected_at_divergence}, got {actual_at_divergence}"
                ),
                **safety_result.detail,
            },
        )
    retry_loops = _retry_loops(steps)
    if retry_loops:
        return ScorerResult(
            name="tool_trajectory",
            score=0.0,
            passed=False,
            detail={
                "expected": expected_text,
                "actual": actual_text,
                "failure": "retry loop detected",
                "retry_loops": retry_loops,
                "error": "tool retry loop detected",
            },
        )
    if not invocation.ok:
        return ScorerResult(
            name="tool_trajectory",
            score=0.0,
            passed=False,
            detail={
                "expected": expected_text,
                "actual": actual_text,
                "failure": "invocation failed before completing the expected trajectory",
                "error": "invocation failed",
            },
        )
    order_mode = getattr(context.expect, "tool_order", "ordered")
    trajectory_matches = Counter(actual) == Counter(expected) if order_mode == "unordered" else actual == expected
    redundant_calls = _redundant_calls(steps, expected)
    recovery = _recovery(steps, invocation.final_text)
    if trajectory_matches or (not expected and forbidden):
        if any(item["status"] == "unrecovered" for item in recovery):
            return ScorerResult(
                name="tool_trajectory",
                score=0.0,
                passed=False,
                detail={
                    "expected": expected_text,
                    "actual": actual_text,
                    "failure": "tool error was not recovered",
                    "recovery": recovery,
                    "error": "tool error was not recovered",
                },
            )
        argument_mismatches = _argument_mismatches(context, steps, expected, order_mode)
        if argument_mismatches:
            first = argument_mismatches[0]
            return ScorerResult(
                name="tool_trajectory",
                score=0.0,
                passed=False,
                detail={
                    "expected": expected_text,
                    "actual": actual_text,
                    "failure": f"position {first['position']}: {first['tool']} arguments differ",
                    "argument_mismatches": argument_mismatches,
                    "error": "tool trajectory arguments diverged",
                },
            )
        return ScorerResult(
            name="tool_trajectory",
            score=1.0,
            passed=True,
            detail={"recovery": recovery} if recovery else {},
        )

    if order_mode == "unordered":
        missing = list((Counter(expected) - Counter(actual)).elements())
        unexpected = list((Counter(actual) - Counter(expected)).elements())
        failures: list[str] = []
        if missing:
            failures.append(f"missing {', '.join(missing)}")
        if unexpected:
            failures.append(f"unexpected {', '.join(unexpected)}")
        unordered_detail: dict[str, Any] = {
            "expected": expected_text,
            "actual": actual_text,
            "missing": missing,
            "unexpected": unexpected,
            "failure": "; ".join(failures),
            "error": "tool trajectory diverged",
        }
        if redundant_calls:
            unordered_detail["redundant_calls"] = redundant_calls
        if recovery:
            unordered_detail["recovery"] = recovery
        return ScorerResult(
            name="tool_trajectory",
            score=0.0,
            passed=False,
            detail=unordered_detail,
        )

    position = next(
        (index for index, pair in enumerate(zip(expected, actual, strict=False)) if pair[0] != pair[1]),
        min(len(expected), len(actual)),
    )
    expected_at_position = expected[position] if position < len(expected) else "(none)"
    actual_at_position = actual[position] if position < len(actual) else "(none)"
    detail: dict[str, Any] = {
        "expected": expected_text,
        "actual": actual_text,
        "failure": (f"position {position + 1}: expected {expected_at_position}, got {actual_at_position}"),
        "error": "tool trajectory diverged",
    }
    if redundant_calls:
        detail["redundant_calls"] = redundant_calls
    if recovery:
        detail["recovery"] = recovery
    return ScorerResult(
        name="tool_trajectory",
        score=0.0,
        passed=False,
        detail=detail,
    )


def _trajectory_label(step: Any) -> str:
    if step.mode == "block":
        return f"{step.name} (BLOCKED)"
    if step.error:
        return f"{step.name} (ERROR)"
    return str(step.name)


def _argument_mismatches(
    context: ScorerContext,
    steps: list[Any],
    expected: list[str],
    order_mode: str,
) -> list[dict[str, Any]]:
    expected_arguments = list(getattr(context.expect, "tool_arguments", None) or [])
    if not expected_arguments:
        return []
    if order_mode == "ordered":
        paired_steps = steps
    else:
        actual_by_name: dict[str, list[Any]] = {}
        for step in steps:
            actual_by_name.setdefault(step.name, []).append(step)
        occurrences: Counter[str] = Counter()
        paired_steps = []
        for name in expected:
            paired_steps.append(actual_by_name[name][occurrences[name]])
            occurrences[name] += 1

    mismatches: list[dict[str, Any]] = []
    for position, (name, expected_args, step) in enumerate(
        zip(expected, expected_arguments, paired_steps, strict=True),
        start=1,
    ):
        actual_args = step.input if isinstance(step.input, dict) else {}
        if actual_args != expected_args:
            mismatches.append(
                {
                    "position": position,
                    "tool": name,
                    "expected": expected_args,
                    "actual": actual_args,
                }
            )
    return mismatches


def _redundant_calls(steps: list[Any], expected: list[str]) -> list[dict[str, Any]]:
    positions_by_signature: dict[tuple[str, str], list[int]] = {}
    for position, step in enumerate(steps, start=1):
        if step.error or step.mode == "block":
            continue
        arguments = step.input if isinstance(step.input, dict) else {}
        signature = (step.name, json.dumps(arguments, sort_keys=True, default=str))
        positions_by_signature.setdefault(signature, []).append(position)
    expected_counts = Counter(expected)
    return [
        {"tool": tool, "positions": positions}
        for (tool, _arguments), positions in positions_by_signature.items()
        if len(positions) > max(1, expected_counts[tool])
    ]


def _retry_loops(steps: list[Any]) -> list[dict[str, Any]]:
    loops: list[dict[str, Any]] = []
    start = 0
    while start < len(steps):
        first = steps[start]
        first_arguments = first.input if isinstance(first.input, dict) else {}
        signature = (first.name, json.dumps(first_arguments, sort_keys=True, default=str))
        end = start + 1
        while end < len(steps):
            candidate = steps[end]
            candidate_arguments = candidate.input if isinstance(candidate.input, dict) else {}
            candidate_signature = (
                candidate.name,
                json.dumps(candidate_arguments, sort_keys=True, default=str),
            )
            if candidate_signature != signature:
                break
            end += 1
        run = steps[start:end]
        if len(run) >= 3 and sum(bool(step.error) for step in run) >= 2:
            loops.append(
                {
                    "tool": first.name,
                    "positions": list(range(start + 1, end + 1)),
                    "attempts": len(run),
                }
            )
        start = end
    return loops


def _recovery(steps: list[Any], final_text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for position, step in enumerate(steps, start=1):
        if not step.error:
            continue
        recovered_at = next(
            (
                (later_position, later)
                for later_position, later in enumerate(steps[position:], start=position + 1)
                if not later.error and later.mode != "block"
            ),
            None,
        )
        finding: dict[str, Any] = {
            "failed_tool": step.name,
            "position": position,
        }
        if recovered_at is not None:
            recovery_position, recovery_step = recovered_at
            finding.update(
                {
                    "status": "recovered",
                    "recovery_tool": recovery_step.name,
                    "recovery_position": recovery_position,
                }
            )
        elif final_text.strip():
            finding.update({"status": "recovered", "recovery_response": True})
        else:
            finding["status"] = "unrecovered"
        findings.append(finding)
    return findings


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


async def llm_judge(context: ScorerContext) -> ScorerResult:
    if context.judge is None:
        return _failed("llm_judge", "judge runtime unavailable")
    return await context.judge.score(context)


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


_DEFAULT_SCORERS: tuple[tuple[str, ScoreFunction], ...] = (
    ("exact_match", exact_match),
    ("regex", regex),
    ("json_schema", json_schema),
    ("expected_tool_call", expected_tool_call),
    ("safety", safety),
    ("tool_trajectory", tool_trajectory),
    ("task_completion", task_completion),
    ("latency", latency),
    ("tokens", tokens),
    ("cost", cost),
    ("llm_judge", llm_judge),
    ("run_quality", run_quality),
)

for _name, _scorer in _DEFAULT_SCORERS:
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
