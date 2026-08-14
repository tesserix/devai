"""Running an agent inside its pinned sandbox and getting a trace back.

This is the loop the platform exists for: author an agent, invoke it under a
pinned configuration, read what it actually did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, LLMUsage, ToolCall
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.models import (
    AgentRef,
    ModelRef,
    SandboxRecord,
    SandboxSpec,
    SandboxStatus,
    ToolMode,
    ToolPolicy,
)
from devai.sandbox.trace import TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry

_SPEC_YAML = """
name: release_notes_writer
display_name: Release Notes Writer
llm_provider: claude
allowed_tools:
  - scm_list_files
  - scm_create_pr
system_prompt: |
  You write release notes.
"""


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(self, request):  # type: ignore[override]
        self.calls += 1
        return self._responses.pop(0)


def _record(**kw) -> SandboxRecord:
    now = datetime.now(UTC)
    spec = SandboxSpec(
        agent=AgentRef(name=kw.get("agent", "release-notes-writer"), version="v1"),
        model=ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
        tools=kw.get("tools", ToolPolicy(default_mode=ToolMode.MOCK)),
        adk_version="0.2.0",
    )
    return SandboxRecord(
        id=kw.get("id", "sb-1"),
        owner="sam@example.com",
        spec=spec,
        status=kw.get("status", SandboxStatus.READY),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _registry() -> SpecializationRegistry:
    reg = SpecializationRegistry()
    reg.register(load_specialization_from_string(_SPEC_YAML))
    return reg


def _invoker(llm, *, registry=None, store=None) -> SandboxInvoker:
    return SandboxInvoker(
        specializations=registry if registry is not None else _registry(),
        deps=StageDeps(config=Settings(), llm=llm),
        traces=store or TraceStore(None),
    )


async def test_one_turn_produces_a_trace_with_the_final_answer() -> None:
    llm = _ScriptedLLM([LLMResponse(text="Here are the notes.")])

    inv = await _invoker(llm).invoke(_record(), message="summarise the diff", triggered_by="sam@example.com")

    assert inv.ok
    assert inv.final_text == "Here are the notes."
    assert inv.agent == "release_notes_writer"
    assert [s.kind for s in inv.steps][0] == "prompt"
    assert [s.kind for s in inv.steps][-1] == "response"


async def test_the_llm_step_carries_the_tokens_the_metrics_are_built_from() -> None:
    llm = _ScriptedLLM([LLMResponse(text="done", usage=LLMUsage(prompt_tokens=120, completion_tokens=40))])

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert inv.totals["total_tokens"] == 160
    assert inv.totals["llm_calls"] == 1


async def test_a_tool_call_is_recorded_with_the_mode_that_served_it() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})]),
            LLMResponse(text="done"),
        ]
    )

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    tool_steps = [s for s in inv.steps if s.kind == "tool"]
    assert [s.name for s in tool_steps] == ["scm_list_files"]
    assert tool_steps[0].mode == "mock"
    assert inv.totals["tool_calls"] == 1


async def test_a_side_effecting_tool_is_blocked_by_the_pinned_policy() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_create_pr", arguments={})]),
            LLMResponse(text="ok"),
        ]
    )
    record = _record(tools=ToolPolicy(default_mode=ToolMode.BLOCK))

    inv = await _invoker(llm).invoke(record, message="ship it", triggered_by="sam@example.com")

    assert inv.totals["blocked_tool_calls"] == 1


async def test_a_tool_outside_the_role_allowlist_never_reaches_the_gateway() -> None:
    # Defence in depth: the role's own allowlist denies first, so the gateway
    # only ever arbitrates tools the agent was entitled to ask for.
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="shell_exec", arguments={})]),
            LLMResponse(text="ok"),
        ]
    )

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert inv.totals["tool_calls"] == 0


async def test_the_trace_is_retrievable_afterwards() -> None:
    store = TraceStore(None)
    inv = await _invoker(_ScriptedLLM([LLMResponse(text="hi")]), store=store).invoke(
        _record(), message="go", triggered_by="sam@example.com"
    )

    assert (await store.get("sb-1", inv.id)) is not None
    assert [i.id for i in await store.list_for_sandbox("sb-1")] == [inv.id]


async def test_an_unknown_agent_is_refused_before_any_model_call() -> None:
    llm = _ScriptedLLM([])

    with pytest.raises(ValueError, match="nope"):
        await _invoker(llm).invoke(_record(agent="nope"), message="go", triggered_by="sam@example.com")

    assert llm.calls == 0


async def test_a_sandbox_that_is_not_live_cannot_be_invoked() -> None:
    llm = _ScriptedLLM([])

    with pytest.raises(ValueError, match="destroyed"):
        await _invoker(llm).invoke(
            _record(status=SandboxStatus.DESTROYED), message="go", triggered_by="sam@example.com"
        )

    assert llm.calls == 0


async def test_a_model_failure_is_a_failed_trace_not_a_lost_one() -> None:
    class _Boom(LLMAdapter):
        provider_name = "boom"

        async def generate(self, request):  # type: ignore[override]
            raise RuntimeError("upstream 503")

    store = TraceStore(None)
    inv = await _invoker(_Boom(), store=store).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert not inv.ok
    assert "503" in inv.error
    assert (await store.get("sb-1", inv.id)) is not None
