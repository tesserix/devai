"""Edge-case coverage for the YAML specialization runner."""

from __future__ import annotations

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, ToolCall
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.specialization import run_specialization_stage
from devai.pipeline.types import DevAITask
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.validator import HandoverValidationError

_SPEC = """
name: edge_agent
category: planning
llm_provider: anthropic
allowed_tools: []
max_turns: 2
output_key: edge_agent_output
context_keys:
  - upstream_output
handover_schema:
  summary:
    type: string
    required: true
system_prompt: You are careful.
"""


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self, responses, capture=None):
        self._responses = list(responses)
        self.calls = 0
        self.last_request = None
        self._capture = capture

    async def generate(self, request):  # type: ignore[override]
        self.calls += 1
        self.last_request = request
        # Always return the last response once the queue drains.
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


class _FakeDispatcher:
    def build_tool_specs(self, names):
        return []

    async def execute(self, name, arguments):
        return f"{name}-ok"


def _deps(llm) -> StageDeps:
    reg = SpecializationRegistry()
    reg.register(load_specialization_from_string(_SPEC))
    return StageDeps(
        config=Settings(),
        llm=llm,
        extra={"specialization_registry": reg, "tool_dispatcher": _FakeDispatcher()},
    )


async def test_strict_handover_raises_on_missing_required_field():
    # Model returns prose with no `summary` → required field missing.
    llm = _ScriptedLLM([LLMResponse(text="no json here")])
    stage = run_specialization_stage(_deps(llm), {"specialization": "edge_agent", "strict_handover": "true"})
    with pytest.raises(HandoverValidationError):
        await stage.execute(DevAITask(intent="x"))


async def test_non_strict_handover_warns_but_returns():
    llm = _ScriptedLLM([LLMResponse(text="no json here")])
    stage = run_specialization_stage(_deps(llm), {"specialization": "edge_agent"})
    result = await stage.execute(DevAITask(intent="x"))
    # Falls back to {"text": ...}; no raise.
    assert result.data["edge_agent_output"] == {"text": "no json here"}


async def test_max_turns_caps_the_tool_loop():
    # Model keeps calling tools forever; max_turns=2 must stop it.
    llm = _ScriptedLLM([LLMResponse(tool_calls=[ToolCall(id="c", name="x", arguments={})])])
    stage = run_specialization_stage(_deps(llm), {"specialization": "edge_agent"})
    result = await stage.execute(DevAITask(intent="x"))
    assert llm.calls == 2  # capped at max_turns, not infinite
    # Never produced final text → empty-text handover fallback.
    assert result.data["edge_agent_output"] == {"text": ""}


async def test_context_keys_are_injected_into_prompt():
    llm = _ScriptedLLM([LLMResponse(text='{"summary": "ok"}')])
    stage = run_specialization_stage(_deps(llm), {"specialization": "edge_agent"})
    task = DevAITask(intent="do the thing")
    task.agent_context["upstream_output"] = "PRIOR-STAGE-DATA"
    await stage.execute(task)
    user_msg = llm.last_request.messages[0].content
    assert "do the thing" in user_msg
    assert "PRIOR-STAGE-DATA" in user_msg
    # The output-contract hint (handover_schema keys) lives in the system prompt.
    assert "summary" in (llm.last_request.system or "")


async def test_unknown_specialization_returns_error():
    llm = _ScriptedLLM([LLMResponse(text="{}")])
    stage = run_specialization_stage(_deps(llm), {"specialization": "ghost"})
    result = await stage.execute(DevAITask(intent="x"))
    assert "not found" in result.message
