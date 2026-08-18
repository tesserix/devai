"""Checks turn a sandbox chat into a repeatable test.

A single turn tells you what the agent said once. A suite tells you whether the
definition still behaves after you changed the prompt — which is the question a
studio actually has to answer before publishing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, LLMUsage, ToolCall
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.evals import EvalCase, EvalExpect, EvalRunner, EvalStore, grade
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.trace import Invocation, TraceStep, TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry

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


def _runner(llm) -> EvalRunner:
    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_SPEC_YAML))
    invoker = SandboxInvoker(
        specializations=_Specs(registry),
        deps=StageDeps(config=Settings(), llm=llm),
        traces=TraceStore(None),
    )
    return EvalRunner(invoker, EvalStore(None))


def _invocation(**kw) -> Invocation:
    inv = Invocation(id="inv-1", sandbox_id="sb-1", agent="a", final_text=kw.get("final_text", ""))
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
async def test_a_run_is_readable_afterwards_so_two_runs_can_be_compared() -> None:
    store = EvalStore(None)
    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_SPEC_YAML))
    runner = EvalRunner(
        SandboxInvoker(
            specializations=_Specs(registry),
            deps=StageDeps(config=Settings(), llm=_ScriptedLLM([LLMResponse(text="ok")])),
            traces=TraceStore(None),
        ),
        store,
    )

    run = await runner.run(_record(), [EvalCase(name="c1", input="go")], triggered_by="sam@example.com")

    assert (await store.get("sb-1", run.id)) is not None
    assert [r.id for r in await store.list_for_sandbox("sb-1")] == [run.id]


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
