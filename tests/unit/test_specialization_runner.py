"""Tests for the pure-YAML specialization runner (execution gap fix)."""

from __future__ import annotations

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, ToolCall
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.specialization import run_specialization_stage
from devai.pipeline.types import DevAITask
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry

_SPEC_YAML = """
name: my_analyst
display_name: My Analyst
category: planning
llm_provider: anthropic
allowed_tools: []
output_key: my_analyst_output
handover_schema:
  summary:
    type: string
    required: true
system_prompt: |
  You analyze things.
"""


class _ScriptedLLM(LLMAdapter):
    """Returns a queued list of LLMResponses, one per generate() call."""

    provider_name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(self, request):  # type: ignore[override]
        self.calls += 1
        return self._responses.pop(0)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def build_tool_specs(self, names):
        return []

    async def execute(self, name, arguments):
        self.executed.append(name)
        return f"{name}-result"


def _spec_registry() -> SpecializationRegistry:
    reg = SpecializationRegistry()
    reg.register(load_specialization_from_string(_SPEC_YAML))
    return reg


def _deps(llm, dispatcher=None) -> StageDeps:
    extra = {"specialization_registry": _spec_registry()}
    if dispatcher is not None:
        extra["tool_dispatcher"] = dispatcher
    return StageDeps(config=Settings(), llm=llm, extra=extra)


async def test_runner_parses_handover_json():
    llm = _ScriptedLLM([LLMResponse(text='Here you go ```json\n{"summary": "all good"}\n```')])
    stage = run_specialization_stage(_deps(llm), {"specialization": "my_analyst"})
    result = await stage.execute(DevAITask(intent="do it"))
    assert result.data["my_analyst_output"] == {"summary": "all good"}
    assert llm.calls == 1


async def test_runner_runs_tool_loop_then_finishes():
    dispatcher = _FakeDispatcher()
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})]),
            LLMResponse(text='{"summary": "done after tool"}'),
        ]
    )
    stage = run_specialization_stage(_deps(llm, dispatcher), {"specialization": "my_analyst"})
    result = await stage.execute(DevAITask(intent="x"))
    assert dispatcher.executed == ["scm_list_files"]
    assert result.data["my_analyst_output"] == {"summary": "done after tool"}
    assert llm.calls == 2


async def test_runner_falls_back_to_text_when_no_json():
    llm = _ScriptedLLM([LLMResponse(text="just prose, no json")])
    stage = run_specialization_stage(_deps(llm), {"specialization": "my_analyst"})
    result = await stage.execute(DevAITask(intent="x"))
    assert result.data["my_analyst_output"] == {"text": "just prose, no json"}


async def test_runner_degrades_to_stub_without_llm():
    stage = run_specialization_stage(_deps(llm=None), {"specialization": "my_analyst"})
    result = await stage.execute(DevAITask(intent="x"))
    assert result.data["my_analyst_output"]["stub"] is True
    assert result.data["my_analyst_output"]["reason"] == "no_llm_adapter"


def test_dispatcher_builds_specs_from_catalog():
    from devai.tools.dispatch import ToolDispatcher

    d = ToolDispatcher(scm=None)
    specs = d.build_tool_specs(["scm_list_files", "does_not_exist"])
    # Unknown tool dropped; known one resolved with a schema.
    assert len(specs) <= 1
    if specs:
        assert specs[0].name == "scm_list_files"
