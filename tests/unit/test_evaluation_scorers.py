from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.evaluations.scorers import ScorerContext, ScorerRegistry, bind, known, run_quality
from devai.sandbox.evals import EvalExpect
from devai.sandbox.trace import Invocation, TraceStep


def _invocation(
    text: str = "done",
    *,
    ok: bool = True,
    steps: list[TraceStep] | None = None,
) -> Invocation:
    invocation = Invocation(
        id="inv-1",
        sandbox_id="sb-1",
        agent="release-notes",
        final_text=text,
        steps=steps or [],
        wall_clock_ms=125,
    )
    invocation.ok = ok
    return invocation


def test_registry_registers_and_binds_scorers_in_requested_order() -> None:
    registry = ScorerRegistry()
    registry.register("first", lambda _context: None)  # type: ignore[arg-type]
    registry.register("second", lambda _context: None)  # type: ignore[arg-type]

    assert [scorer.name for scorer in registry.bind(["second", "first", "second"])] == ["second", "first"]


def test_registry_rejects_an_unknown_scorer_instead_of_silently_skipping_it() -> None:
    with pytest.raises(ValueError, match="unknown scorer.*missing"):
        ScorerRegistry().bind(["missing"])


def test_deterministic_scorers_cover_output_schema_tools_and_completion() -> None:
    invocation = _invocation(
        '{"status":"done"}',
        steps=[TraceStep(kind="tool", name="scm_list_files")],
    )
    expect = EvalExpect(
        exact_output='{"status":"done"}',
        matches=r'"status":"done"',
        json_schema={
            "type": "object",
            "properties": {"status": {"const": "done"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        tools_called=["scm_list_files"],
    )

    results = [
        scorer.score(ScorerContext(invocation=invocation, expect=expect))
        for scorer in bind(["exact_match", "regex", "json_schema", "expected_tool_call", "task_completion"])
    ]

    assert [result.name for result in results] == [
        "exact_match",
        "regex",
        "json_schema",
        "expected_tool_call",
        "task_completion",
    ]
    assert all(result.passed and result.score == 1.0 for result in results)


def test_tool_trajectory_keeps_the_existing_suite_name_compatible() -> None:
    invocation = _invocation(steps=[TraceStep(kind="tool", name="scm_list_files")])
    scorer = bind(["tool_trajectory"])[0]

    result = scorer.score(ScorerContext(invocation=invocation, expect=EvalExpect(tools_called=["scm_list_files"])))

    assert result.name == "tool_trajectory"
    assert result.passed


def test_deterministic_scorers_fail_closed_for_bad_output_and_missing_expectations() -> None:
    malformed = _invocation("not-json")

    results = [
        scorer.score(ScorerContext(invocation=malformed, expect=EvalExpect()))
        for scorer in bind(["exact_match", "regex", "json_schema", "expected_tool_call"])
    ]

    assert all(not result.passed and result.score == 0.0 for result in results)
    assert all(result.detail.get("error") for result in results)


def test_operational_scorers_report_raw_measurements_and_apply_case_budgets() -> None:
    invocation = _invocation(
        steps=[TraceStep(kind="llm", prompt_tokens=80, completion_tokens=20, cost_usd=0.04, latency_ms=125)]
    )
    expect = EvalExpect(max_latency_ms=100, max_total_tokens=150, max_cost_usd=0.05)

    results = {
        scorer.name: scorer.score(ScorerContext(invocation=invocation, expect=expect))
        for scorer in bind(["latency", "tokens", "cost"])
    }

    assert results["latency"].score == 125
    assert not results["latency"].passed
    assert results["tokens"].score == 100
    assert results["tokens"].passed
    assert results["cost"].score == pytest.approx(0.04)
    assert results["cost"].passed


def test_run_quality_is_registered_without_changing_the_existing_formula() -> None:
    task = SimpleNamespace(
        pr_number=42,
        context={"deploy_status": "success"},
        stages_completed=["implement", "review", "deploy"],
        stages_failed=[],
        status="completed",
    )

    result = run_quality(ScorerContext(task=task))

    assert "run_quality" in known()
    assert result.name == "run_quality"
    assert result.score == 1.0
    assert result.passed
    assert result.detail["pr_number"] == 42
