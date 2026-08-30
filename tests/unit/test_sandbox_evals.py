"""Checks turn a sandbox chat into a repeatable test.

A single turn tells you what the agent said once. A suite tells you whether the
definition still behaves after you changed the prompt — which is the question a
studio actually has to answer before publishing.
"""

from __future__ import annotations

import asyncio
import json
import unittest.mock
from datetime import UTC, datetime, timedelta

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, LLMUsage, ToolCall
from devai.config import Settings
from devai.evaluations.judge import LLMJudge
from devai.evaluations.models import JudgeConfig, JudgeRubric
from devai.identity import Principal
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.evals import CaseResult, EvalCase, EvalExpect, EvalRun, EvalRunner, EvalStore, grade
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.models import AgentRef, ModelRef, SandboxLimits, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.trace import Invocation, TraceStep, TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from tests.unit.test_sandbox_invoke import _GrantedSandboxLLM

_SPEC_YAML = """
name: release_notes_writer
display_name: Release Notes Writer
llm_provider: claude
allowed_tools:
  - scm_list_files
system_prompt: |
  You write release notes.
"""


class _Specs:
    def __init__(self, registry: SpecializationRegistry) -> None:
        self._registry = registry

    async def resolve_runnable(self, name: str):
        return self._registry.resolve(name) if self._registry.has(name) else None


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def generate(self, request):  # type: ignore[override]
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _record() -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner="sam@example.com",
        spec=SandboxSpec(
            agent=AgentRef(name="release-notes-writer", version="v1"),
            model=ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _runner(llm, **kwargs) -> EvalRunner:
    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_SPEC_YAML))
    invoker = SandboxInvoker(
        specializations=_Specs(registry),
        deps=StageDeps(config=Settings(), llm=llm),
        traces=TraceStore(None),
        credentials=_GrantedSandboxLLM(llm),
    )
    return EvalRunner(invoker, EvalStore(None), **kwargs)


def _invocation(**kw) -> Invocation:
    inv = Invocation(
        id="inv-1",
        sandbox_id="sb-1",
        agent="a",
        final_text=kw.get("final_text", ""),
        execution_backend=kw.get("execution_backend", "inline"),
    )
    inv.ok = kw.get("ok", True)
    inv.steps = kw.get("steps", [])
    return inv


# ── grading ──────────────────────────────────────────────────────────


def test_a_case_passes_when_every_expectation_holds() -> None:
    inv = _invocation(final_text="v2.1 ships the sandbox console.")

    assert grade(EvalExpect(contains=["v2.1", "sandbox"]), inv) == []


def test_a_missing_phrase_is_reported_by_name() -> None:
    inv = _invocation(final_text="nothing to report")

    failures = grade(EvalExpect(contains=["v2.1"]), inv)

    assert failures == ["missing expected text: 'v2.1'"]


def test_a_forbidden_phrase_fails() -> None:
    inv = _invocation(final_text="I cannot help with that.")

    assert grade(EvalExpect(not_contains=["cannot help"]), inv)


def test_matching_is_case_insensitive_because_models_are_not_stable_about_case() -> None:
    inv = _invocation(final_text="V2.1 Ships The Sandbox Console.")

    assert grade(EvalExpect(contains=["v2.1 ships"]), inv) == []


def test_a_regex_expectation_is_applied_to_the_final_text() -> None:
    inv = _invocation(final_text="released 2026-08-14")

    assert grade(EvalExpect(matches=r"\d{4}-\d{2}-\d{2}"), inv) == []
    assert grade(EvalExpect(matches=r"^v\d"), inv)


def test_an_invalid_regex_fails_the_case_instead_of_the_run() -> None:
    failures = grade(EvalExpect(matches="("), _invocation(final_text="x"))

    assert failures and "regex" in failures[0]


def test_a_tool_expectation_reads_the_trace_not_the_answer() -> None:
    inv = _invocation(final_text="done", steps=[TraceStep(kind="tool", name="scm_list_files")])

    assert grade(EvalExpect(tools_called=["scm_list_files"]), inv) == []
    assert grade(EvalExpect(tools_not_called=["scm_list_files"]), inv)


def test_a_budget_is_an_expectation_like_any_other() -> None:
    inv = _invocation(
        final_text="done",
        steps=[TraceStep(kind="llm", prompt_tokens=900, completion_tokens=200, latency_ms=4000)],
    )

    assert grade(EvalExpect(max_total_tokens=500), inv)
    assert grade(EvalExpect(max_latency_ms=1000), inv)
    assert grade(EvalExpect(max_total_tokens=2000, max_latency_ms=9000), inv) == []


def test_a_failed_invocation_fails_the_case_even_with_no_expectations() -> None:
    inv = _invocation(ok=False)
    inv.error = "upstream 503"

    assert grade(EvalExpect(), inv) == ["run failed: upstream 503"]


def test_eval_summary_reports_p95_wall_clock_across_cases() -> None:
    run = EvalRun(
        id="eval-1",
        sandbox_id="sb-1",
        results=[
            CaseResult(name=f"case-{latency}", passed=True, totals={"wall_clock_ms": latency})
            for latency in range(10, 201, 10)
        ],
    )

    assert run.summary["p95_latency_ms"] == 190


# ── running a suite ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_suite_runs_every_case_and_summarises_the_outcome() -> None:
    llm = _ScriptedLLM([LLMResponse(text="v2.1 ships the sandbox console.", usage=LLMUsage(prompt_tokens=10))])
    cases = [
        EvalCase(name="mentions the version", input="summarise", expect=EvalExpect(contains=["v2.1"])),
        EvalCase(name="stays on topic", input="summarise", expect=EvalExpect(not_contains=["sandbox"])),
    ]

    run = await _runner(llm).run(_record(), cases, triggered_by="sam@example.com")

    assert [r.name for r in run.results] == ["mentions the version", "stays on topic"]
    assert [r.passed for r in run.results] == [True, False]
    assert run.summary["passed"] == 1
    assert run.summary["failed"] == 1
    assert run.summary["pass_rate"] == 0.5
    assert run.summary["total_tokens"] == 20
    assert run.summary["cost_breakdown"] == {
        "agent_cost_usd": pytest.approx(0.00006),
        "judge_cost_usd": 0.0,
        "infrastructure_cost_usd": 0.0,
    }
    assert run.summary["cost_usd"] == pytest.approx(0.00006)


@pytest.mark.asyncio
async def test_each_case_keeps_the_trace_it_was_judged_on() -> None:
    llm = _ScriptedLLM([LLMResponse(text="ok")])

    run = await _runner(llm).run(_record(), [EvalCase(name="c1", input="go")], triggered_by="sam@example.com")

    assert run.results[0].invocation_id.startswith("inv-")


@pytest.mark.asyncio
async def test_a_broken_case_fails_without_taking_the_suite_down() -> None:
    class _Boom(LLMAdapter):
        provider_name = "boom"

        async def generate(self, request):  # type: ignore[override]
            raise RuntimeError("upstream 503")

    run = await _runner(_Boom()).run(
        _record(),
        [EvalCase(name="c1", input="go"), EvalCase(name="c2", input="go")],
        triggered_by="sam@example.com",
    )

    assert run.summary["failed"] == 2
    assert "503" in run.results[0].failures[0]


@pytest.mark.asyncio
async def test_a_suite_needs_at_least_one_case() -> None:
    with pytest.raises(ValueError, match="at least one case"):
        await _runner(_ScriptedLLM([])).run(_record(), [], triggered_by="sam@example.com")


@pytest.mark.asyncio
async def test_a_suite_refuses_more_cases_than_the_configured_tenant_limit() -> None:
    cases = [EvalCase(name="c1", input="go"), EvalCase(name="c2", input="go")]

    with pytest.raises(ValueError, match="tenant dataset quota.*1 case"):
        await _runner(_ScriptedLLM([]), max_cases=1).run(_record(), cases, triggered_by="sam@example.com")


@pytest.mark.asyncio
async def test_a_run_is_readable_afterwards_so_two_runs_can_be_compared() -> None:
    store = EvalStore(None)
    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_SPEC_YAML))
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    runner = EvalRunner(
        SandboxInvoker(
            specializations=_Specs(registry),
            deps=StageDeps(config=Settings(), llm=llm),
            traces=TraceStore(None),
            credentials=_GrantedSandboxLLM(llm),
        ),
        store,
    )

    run = await runner.run(_record(), [EvalCase(name="c1", input="go")], triggered_by="sam@example.com")

    assert (await store.get("sb-1", run.id)) is not None
    assert [r.id for r in await store.list_for_sandbox("sb-1")] == [run.id]
    assert run.configuration["agent"] == {"name": "release-notes-writer", "version": "v1"}
    assert run.configuration["model"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
    }


@pytest.mark.asyncio
async def test_global_run_lookup_is_scoped_to_the_server_derived_owner() -> None:
    store = EvalStore(None)
    run = EvalRun(id="eval-1", sandbox_id="sb-1", owner_scope="tenant-a:alice")
    await store.save(run, ttl_seconds=300)

    assert await store.get_by_id("tenant-a:alice", "eval-1") is not None
    assert await store.get_by_id("tenant-a:bob", "eval-1") is None


@pytest.mark.asyncio
async def test_retrying_a_durable_eval_returns_the_saved_run_without_invoking_again() -> None:
    class _CountingInvoker:
        calls = 0

        async def invoke(self, record, *, message: str, triggered_by: str):
            del record, message, triggered_by
            self.calls += 1
            return _invocation(final_text="ok")

    invoker = _CountingInvoker()
    store = EvalStore(None)
    runner = EvalRunner(invoker, store)  # type: ignore[arg-type]
    case = EvalCase(name="works", input="go")

    first = await runner.run(
        _record(),
        [case],
        triggered_by="tenant-a:alice",
        owner_scope="tenant-a:alice",
        run_id="eval-stable",
    )
    second = await runner.run(
        _record(),
        [case],
        triggered_by="tenant-a:alice",
        owner_scope="tenant-a:alice",
        run_id="eval-stable",
    )

    assert first.id == second.id == "eval-stable"
    assert invoker.calls == 1


@pytest.mark.asyncio
async def test_a_tool_calling_case_is_graded_on_the_tool_it_chose() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})]),
            LLMResponse(text="done"),
        ]
    )

    run = await _runner(llm).run(
        _record(),
        [EvalCase(name="uses the repo", input="go", expect=EvalExpect(tools_called=["scm_list_files"]))],
        triggered_by="sam@example.com",
    )

    assert run.results[0].passed


@pytest.mark.asyncio
async def test_a_suite_persists_every_named_scorer_for_every_case() -> None:
    llm = _ScriptedLLM([LLMResponse(text="expected")])
    cases = [
        EvalCase(name="one", input="go", expect=EvalExpect(exact_output="expected")),
        EvalCase(name="two", input="go", expect=EvalExpect(exact_output="different")),
    ]

    run = await _runner(llm).run(
        _record(),
        cases,
        triggered_by="sam@example.com",
        scorers=["exact_match", "task_completion"],
    )

    assert [list(result.scores) for result in run.results] == [
        ["exact_match", "task_completion"],
        ["exact_match", "task_completion"],
    ]
    assert run.results[0].scores["exact_match"] == {
        "name": "exact_match",
        "score": 1.0,
        "passed": True,
        "unit": "ratio",
        "detail": {},
    }
    assert not run.results[1].passed
    assert run.summary["dimensions"] == {
        "exact_match": {"average": 0.5, "passed": 1, "failed": 1, "pass_rate": 0.5, "unit": "ratio"},
        "task_completion": {"average": 1.0, "passed": 2, "failed": 0, "pass_rate": 1.0, "unit": "ratio"},
    }


@pytest.mark.asyncio
async def test_a_correct_answer_reached_through_a_forbidden_path_fails_the_eval() -> None:
    class _ForbiddenPathInvoker:
        async def invoke(self, record, *, message: str, triggered_by: str) -> Invocation:
            del message, triggered_by
            return Invocation(
                id="inv-forbidden",
                sandbox_id=record.id,
                agent="support-agent",
                final_text="refund complete",
                steps=[
                    TraceStep(kind="tool", name="customer_search"),
                    TraceStep(kind="tool", name="refund", mode="block"),
                ],
            )

    run = await EvalRunner(_ForbiddenPathInvoker(), EvalStore(None)).run(
        _record(),
        [
            EvalCase(
                name="refund-safety",
                input="refund order 4471",
                expect=EvalExpect(
                    exact_output="refund complete",
                    tools_called=["customer_search", "eligibility_check", "refund"],
                    tools_not_called=["refund"],
                ),
            )
        ],
        triggered_by="sam@example.com",
        scorers=["exact_match", "tool_trajectory", "safety"],
    )

    result = run.results[0]
    assert result.scores["exact_match"]["passed"]
    assert not result.scores["tool_trajectory"]["passed"]
    assert not result.scores["safety"]["passed"]
    assert not result.passed
    assert "tool_trajectory: position 2: expected eligibility_check, got refund (BLOCKED)" in result.failures


@pytest.mark.asyncio
async def test_case_execution_is_bounded_and_result_order_is_stable() -> None:
    class _BlockingInvoker:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0
            self.two_started = asyncio.Event()
            self.release = asyncio.Event()

        async def invoke(self, record, *, message: str, triggered_by: str) -> Invocation:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.two_started.set()
            await self.release.wait()
            self.active -= 1
            return Invocation(id=f"inv-{message}", sandbox_id=record.id, agent="agent", final_text=message)

    invoker = _BlockingInvoker()
    runner = EvalRunner(invoker, EvalStore(None), max_concurrency=2)  # type: ignore[arg-type]
    cases = [EvalCase(name=f"case-{index}", input=str(index)) for index in range(4)]

    running = asyncio.create_task(runner.run(_record(), cases, triggered_by="sam@example.com"))
    await asyncio.wait_for(invoker.two_started.wait(), timeout=1)
    assert invoker.peak == 2
    invoker.release.set()

    run = await running
    assert invoker.peak == 2
    assert [result.name for result in run.results] == ["case-0", "case-1", "case-2", "case-3"]
    assert {result.execution_backend for result in run.results} == {"inline"}


@pytest.mark.asyncio
async def test_fifty_case_acceptance_summary_keeps_scores_cost_latency_and_trace_links() -> None:
    class _FiftyCaseInvoker:
        def __init__(self) -> None:
            self.count = 0

        async def invoke(self, record, *, message: str, triggered_by: str) -> Invocation:
            del triggered_by
            self.count += 1
            return Invocation(
                id=f"inv-{message}",
                sandbox_id=record.id,
                agent="agent",
                final_text="done",
                execution_backend="kubernetes_job",
                wall_clock_ms=self.count,
                steps=[TraceStep(kind="llm", cost_usd=0.01)],
            )

    cases = [EvalCase(name=f"case-{index}", input=str(index)) for index in range(50)]
    run = await EvalRunner(_FiftyCaseInvoker(), EvalStore(None)).run(
        _record(),
        cases,
        triggered_by="sam@example.com",
        scorers=["task_completion", "cost"],
    )

    assert run.summary["pass_rate"] == 1.0
    assert run.summary["p95_latency_ms"] == 48
    assert run.summary["cost_usd"] == 0.5
    assert run.summary["dimensions"]["task_completion"]["pass_rate"] == 1.0
    assert all(result.to_dict()["trace_url"] for result in run.results)
    assert {result.execution_backend for result in run.results} == {"kubernetes_job"}


@pytest.mark.asyncio
async def test_judge_budget_exhaustion_fails_only_the_judge_dimension_after_agent_calls() -> None:
    class _Invoker:
        def __init__(self) -> None:
            self.count = 0

        async def invoke(self, record, *, message: str, triggered_by: str) -> Invocation:
            del triggered_by
            self.count += 1
            return Invocation(
                id=f"inv-{message}",
                sandbox_id=record.id,
                agent="agent",
                message=message,
                final_text="expected",
                steps=[TraceStep(kind="llm", cost_usd=0.095)],
            )

    class _Factory:
        def __init__(self, invoker: _Invoker) -> None:
            self.invoker = invoker

        async def create(self, *, principal, config, budget, metadata):
            assert self.invoker.count == 1
            assert principal.email == "alice@example.com"
            return (
                LLMJudge(
                    llm=_ScriptedLLM([LLMResponse(text="{}")]),
                    config=config,
                    budget=budget,
                    metadata=metadata,
                ),
                config,
            )

    config = JudgeConfig(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        rubric=JudgeRubric(name="quality", version="3", dimensions={"helpfulness": "Be useful."}),
        max_cost_per_case_usd=0.01,
    )
    record = _record().model_copy(
        update={"spec": _record().spec.model_copy(update={"limits": SandboxLimits(max_cost_usd=0.1)})}
    )
    invoker = _Invoker()
    run = await EvalRunner(invoker, EvalStore(None), judge_factory=_Factory(invoker)).run(
        record,
        [EvalCase(name="one", input="go", expect=EvalExpect(exact_output="expected"))],
        triggered_by="alice@example.com",
        scorers=["exact_match", "llm_judge"],
        principal=Principal(uid="alice", email="alice@example.com", tenant_id="tenant-a"),
        judge_config=config,
    )

    assert run.results[0].scores["exact_match"]["passed"] is True
    assert run.results[0].scores["llm_judge"]["detail"]["error"] == "judge cost budget exhausted"
    assert run.results[0].passed is False
    assert run.judge_cost_usd == 0
    assert run.judge == config.model_dump(mode="json")


@pytest.mark.asyncio
async def test_scoring_outage_preserves_agent_output_and_retry_does_not_reinvoke_tools() -> None:
    class _Invoker:
        calls = 0

        async def invoke(self, record, *, message: str, triggered_by: str) -> Invocation:
            del triggered_by
            self.calls += 1
            return Invocation(
                id="inv-preserved",
                sandbox_id=record.id,
                agent="agent",
                message=message,
                final_text="expected",
            )

    class _UnavailableJudge:
        async def create(self, **_: object) -> object:
            raise ConnectionError("judge provider unavailable")

    config = JudgeConfig(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        rubric=JudgeRubric(name="quality", version="3", dimensions={"helpfulness": "Be useful."}),
    )
    invoker = _Invoker()
    store = EvalStore(None)
    runner = EvalRunner(invoker, store, judge_factory=_UnavailableJudge())  # type: ignore[arg-type]
    request = {
        "record": _record(),
        "cases": [EvalCase(name="one", input="go", expect=EvalExpect(exact_output="expected"))],
        "triggered_by": "alice@example.com",
        "owner_scope": "tenant-a:alice",
        "scorers": ["exact_match", "llm_judge"],
        "principal": Principal(uid="alice", email="alice@example.com", tenant_id="tenant-a"),
        "judge_config": config,
        "run_id": "eval-scoring-outage",
    }

    first = await runner.run(**request)  # type: ignore[arg-type]
    second = await runner.run(**request)  # type: ignore[arg-type]

    assert first.id == second.id == "eval-scoring-outage"
    assert invoker.calls == 1
    assert first.results[0].invocation_id == "inv-preserved"
    assert first.results[0].scores["exact_match"]["passed"] is True
    assert first.results[0].scores["llm_judge"]["detail"]["error"] == "judge runtime unavailable"


@pytest.mark.asyncio
async def test_judge_calibration_reports_threshold_agreement_and_mean_absolute_error() -> None:
    config = JudgeConfig(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        rubric=JudgeRubric(
            name="quality",
            version="3",
            dimensions={"helpfulness": "Be useful.", "groundedness": "Use the evidence."},
        ),
    )
    response = LLMResponse(
        text=json.dumps(
            {
                "dimensions": {
                    "helpfulness": {"score": 0.8, "reasoning": "Actionable."},
                    "groundedness": {"score": 0.4, "reasoning": "One unsupported claim."},
                }
            }
        ),
        usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        provider="anthropic",
        model="claude-sonnet-4-20250514",
    )

    class _Factory:
        async def create(self, *, principal, config, budget, metadata):
            del principal
            return LLMJudge(llm=_ScriptedLLM([response]), config=config, budget=budget, metadata=metadata), config

    run = await EvalRunner(
        _runner(_ScriptedLLM([LLMResponse(text="answer")]))._invoker,
        EvalStore(None),
        judge_factory=_Factory(),
    ).run(
        _record(),
        [
            EvalCase(
                name="labelled",
                input="go",
                human_scores={"helpfulness": 1.0, "groundedness": 0.2},
            )
        ],
        triggered_by="alice@example.com",
        scorers=["task_completion", "llm_judge"],
        principal=Principal(uid="alice", email="alice@example.com", tenant_id="tenant-a"),
        judge_config=config,
    )

    assert run.summary["calibration"] == {
        "labelled_scores": 2,
        "threshold_agreement": 1.0,
        "mean_absolute_error": 0.2,
    }
    assert run.judge_cost_usd > 0


# ── background execution ─────────────────────────────────────────────


async def test_start_answers_running_immediately_and_completes_in_the_background() -> None:
    llm = _ScriptedLLM([LLMResponse(text="v2.1 ships.")])
    runner = _runner(llm)

    run = await runner.start(
        _record(),
        [EvalCase(name="c1", input="summarise", expect=EvalExpect(contains=["v2.1"]))],
        triggered_by="sam@example.com",
    )

    assert run.status == "running"
    # Persisted before the first case executed, so a poll can always find it.
    stored = await runner.store.get("sb-1", run.id)
    assert stored is not None
    await asyncio.gather(*runner._tasks)
    finished = await runner.store.get("sb-1", run.id)
    assert finished is not None
    assert finished.status == "completed"
    assert finished.summary["status"] == "completed"
    assert [r.passed for r in finished.results] == [True]


async def test_a_background_suite_that_blows_up_lands_in_failed_not_limbo() -> None:
    class _ExplodingInvoker:
        async def invoke(self, record, *, message, triggered_by):
            raise RuntimeError("cluster gone")

    runner = EvalRunner(_ExplodingInvoker(), EvalStore(None))
    with unittest.mock.patch.object(EvalRunner, "_execute", side_effect=RuntimeError("storage write refused")):
        run = await runner.start(_record(), [EvalCase(name="c1", input="go")], triggered_by="sam@example.com")
        await asyncio.gather(*runner._tasks, return_exceptions=True)
    stored = await runner.store.get("sb-1", run.id)
    assert stored is not None
    assert stored.status == "failed"
    assert "storage write refused" in stored.error


async def test_double_save_keeps_one_index_entry_per_run() -> None:
    llm = _ScriptedLLM([LLMResponse(text="ok")])
    runner = _runner(llm)

    run = await runner.start(_record(), [EvalCase(name="c1", input="go")], triggered_by="sam@example.com")
    await asyncio.gather(*runner._tasks)

    listed = await runner.store.list_for_sandbox("sb-1", limit=20)
    assert [r.id for r in listed].count(run.id) == 1
