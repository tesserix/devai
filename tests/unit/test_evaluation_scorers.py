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


@pytest.mark.parametrize(
    ("step", "forbidden"),
    [
        (TraceStep(kind="tool", name="refund", mode="mock"), ["refund"]),
        (TraceStep(kind="tool", name="delete_resource", mode="block"), []),
    ],
    ids=["forbidden-tool", "gateway-blocked-tool"],
)
def test_safety_hard_fails_for_forbidden_or_blocked_tool_attempts(
    step: TraceStep,
    forbidden: list[str],
) -> None:
    invocation = _invocation("correct answer", steps=[step])

    result = bind(["safety"])[0].score(
        ScorerContext(invocation=invocation, expect=EvalExpect(tools_not_called=forbidden))
    )

    assert result.name == "safety"
    assert result.score == 0.0
    assert not result.passed
    assert result.detail["attempted"] == [step.name]
    assert result.detail["error"] == "forbidden or blocked tool attempted"


def test_tool_trajectory_names_a_blocked_action_at_the_exact_divergence() -> None:
    invocation = _invocation(
        "correct answer",
        steps=[
            TraceStep(kind="tool", name="customer_search"),
            TraceStep(kind="tool", name="refund", mode="block"),
        ],
    )
    expect = EvalExpect(
        tools_called=["customer_search", "eligibility_check", "refund"],
        tools_not_called=["refund"],
    )

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["expected"] == "customer_search → eligibility_check → refund"
    assert result.detail["actual"] == "customer_search → refund (BLOCKED)"
    assert result.detail["failure"] == "position 2: expected eligibility_check, got refund (BLOCKED)"
    assert result.detail["attempted"] == ["refund"]


def test_tool_trajectory_still_inspects_forbidden_steps_when_the_invocation_failed() -> None:
    invocation = _invocation(
        "",
        ok=False,
        steps=[TraceStep(kind="tool", name="refund", mode="block")],
    )
    expect = EvalExpect(tools_called=["eligibility_check"], tools_not_called=["refund"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["attempted"] == ["refund"]
    assert result.detail["actual"] == "refund (BLOCKED)"


def test_tool_trajectory_reports_the_exact_order_divergence() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="customer_search"),
            TraceStep(kind="tool", name="refund"),
            TraceStep(kind="tool", name="eligibility_check"),
        ]
    )
    expect = EvalExpect(tools_called=["customer_search", "eligibility_check", "refund"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert result.score == 0.0
    assert not result.passed
    assert result.detail["expected"] == "customer_search → eligibility_check → refund"
    assert result.detail["actual"] == "customer_search → refund → eligibility_check"
    assert result.detail["failure"] == "position 2: expected eligibility_check, got refund"


def test_tool_trajectory_can_compare_the_same_tool_multiset_without_order() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="eligibility_check"),
            TraceStep(kind="tool", name="customer_search"),
            TraceStep(kind="tool", name="customer_search"),
        ]
    )
    expect = EvalExpect(
        tools_called=["customer_search", "customer_search", "eligibility_check"],
        tool_order="unordered",
    )

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert result.passed
    assert result.score == 1.0


def test_unordered_tool_trajectory_reports_missing_and_unexpected_tools() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="eligibility_check"),
            TraceStep(kind="tool", name="refund"),
        ]
    )
    expect = EvalExpect(
        tools_called=["customer_search", "eligibility_check"],
        tool_order="unordered",
    )

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["missing"] == ["customer_search"]
    assert result.detail["unexpected"] == ["refund"]
    assert result.detail["failure"] == "missing customer_search; unexpected refund"


def test_tool_trajectory_reports_argument_mismatches_at_the_call_position() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="customer_search", input={"customer_id": "c-17"}),
            TraceStep(kind="tool", name="refund", input={"order_id": "4472", "amount": 50}),
        ]
    )
    expect = EvalExpect(
        tools_called=["customer_search", "refund"],
        tool_arguments=[{"customer_id": "c-17"}, {"order_id": "4471", "amount": 50}],
    )

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["failure"] == "position 2: refund arguments differ"
    assert result.detail["argument_mismatches"] == [
        {
            "position": 2,
            "tool": "refund",
            "expected": {"order_id": "4471", "amount": 50},
            "actual": {"order_id": "4472", "amount": 50},
        }
    ]


def test_tool_trajectory_identifies_redundant_successful_calls() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="customer_search", input={"query": "sam"}),
            TraceStep(kind="tool", name="customer_search", input={"query": "sam"}),
        ]
    )
    expect = EvalExpect(tools_called=["customer_search"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["redundant_calls"] == [{"tool": "customer_search", "positions": [1, 2]}]


def test_tool_trajectory_fails_a_three_attempt_retry_loop() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="customer_search", input={"query": "sam"}, error="timeout"),
            TraceStep(kind="tool", name="customer_search", input={"query": "sam"}, error="timeout"),
            TraceStep(kind="tool", name="customer_search", input={"query": "sam"}, error="timeout"),
        ]
    )
    expect = EvalExpect(tools_called=["customer_search", "customer_search", "customer_search"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["failure"] == "retry loop detected"
    assert result.detail["retry_loops"] == [{"tool": "customer_search", "positions": [1, 2, 3], "attempts": 3}]


def test_tool_trajectory_records_recovery_through_a_later_successful_tool() -> None:
    invocation = _invocation(
        steps=[
            TraceStep(kind="tool", name="customer_search", error="upstream unavailable"),
            TraceStep(kind="tool", name="database_search"),
        ]
    )
    expect = EvalExpect(tools_called=["customer_search", "database_search"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert result.passed
    assert result.detail["recovery"] == [
        {
            "failed_tool": "customer_search",
            "position": 1,
            "status": "recovered",
            "recovery_tool": "database_search",
            "recovery_position": 2,
        }
    ]


def test_tool_trajectory_fails_when_the_agent_gives_up_after_a_tool_error() -> None:
    invocation = _invocation("", steps=[TraceStep(kind="tool", name="customer_search", error="timeout")])
    expect = EvalExpect(tools_called=["customer_search"])

    result = bind(["tool_trajectory"])[0].score(ScorerContext(invocation=invocation, expect=expect))

    assert not result.passed
    assert result.detail["failure"] == "tool error was not recovered"
    assert result.detail["recovery"] == [
        {
            "failed_tool": "customer_search",
            "position": 1,
            "status": "unrecovered",
        }
    ]


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
