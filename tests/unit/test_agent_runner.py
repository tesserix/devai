"""Unit tests for the AgentRunner tool-calling loop.

Uses a FakeLLMAdapter that scripts a sequence of LLMResponses, so the
whole loop runs with no network, no real model, and no cluster. Covers:

  - the no-tool path (final JSON handover parsed + validated)
  - the tool-calling loop (tool dispatched, result threaded back, final JSON)
  - graceful degrade when no LLM adapter is wired
  - handover validation surfacing violations
"""

from __future__ import annotations

import pytest

from devai.adapters.llm.base import LLMResponse, LLMRole, LLMUsage, ToolCall
from devai.agentruntime.runner import AgentRunner
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask
from devai.specializations.base import HandoverField, Specialization


class FakeLLMAdapter:
    """Scripts a list of LLMResponses; records the requests it received."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list = []

    async def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        if not self._responses:
            return LLMResponse(text='{"done": true}', usage=LLMUsage())
        return self._responses.pop(0)


class FakeSCM:
    """Minimal SCM client exposing only what the tools under test call."""

    def __init__(self) -> None:
        self.calls: list = []

    async def get_file_content(self, repo, path, ref=None):  # noqa: ANN001
        self.calls.append((repo, path, ref))
        return "print('hello from %s')" % path


def _deps(llm) -> StageDeps:  # noqa: ANN001
    return StageDeps(config=None, scm=None, state_manager=None, llm=llm)


def _task(**kw) -> DevAITask:
    return DevAITask(intent=kw.pop("intent", "do a thing"), repo=kw.pop("repo", "tesserix/x"), **kw)


@pytest.mark.asyncio
async def test_no_tool_path_parses_and_validates_handover():
    spec = Specialization(
        name="summarizer",
        handover_schema={
            "summary": HandoverField(name="summary", type="string", required=True),
            "count": HandoverField(name="count", type="integer", required=True),
        },
    )
    llm = FakeLLMAdapter(
        [
            LLMResponse(
                text='Here is the result:\n```json\n{"summary": "did it", "count": 3}\n```',
                usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            )
        ]
    )
    run = await AgentRunner(_deps(llm)).run(spec, _task())

    assert run.stub is False
    assert run.turns == 1
    assert run.tool_calls == 0
    assert run.patch == {"summary": "did it", "count": 3}
    assert "_handover_violations" not in run.patch
    assert run.prompt_tokens == 10


@pytest.mark.asyncio
async def test_tool_loop_dispatches_and_threads_result_back(monkeypatch):
    monkeypatch.setenv("DEVAI_SANDBOX_TOOL_MODE", "real")
    spec = Specialization(
        name="reader",
        allowed_tools=["read_file"],
        handover_schema={"summary": HandoverField(name="summary", type="string", required=True)},
    )
    scm = FakeSCM()
    deps = StageDeps(config=None, scm=scm, state_manager=None, llm=None)
    llm = FakeLLMAdapter(
        [
            # turn 1: ask to read a file
            LLMResponse(
                tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "main.py"})],
                finish_reason="tool_use",
            ),
            # turn 2: final answer
            LLMResponse(text='```json\n{"summary": "read main.py"}\n```'),
        ]
    )
    deps = StageDeps(config=None, scm=scm, state_manager=None, llm=llm)

    run = await AgentRunner(deps).run(spec, _task())

    assert run.tool_calls == 1
    assert [{key: value for key, value in step.items() if key != "latency_ms"} for step in run.trace_steps] == [
        {
            "kind": "tool",
            "name": "read_file",
            "input": {"path": "main.py"},
            "output": "print('hello from main.py')",
            "mode": "real",
            "error": "",
        }
    ]
    assert run.trace_steps[0]["latency_ms"] >= 0
    assert run.turns == 2
    assert run.patch == {"summary": "read main.py"}
    # the SCM tool actually executed with repo defaulted from the task
    assert scm.calls == [("tesserix/x", "main.py", None)]
    # the 2nd request must carry: USER, ASSISTANT(with tool_calls), TOOL(result)
    second = llm.requests[1]
    roles = [m.role for m in second.messages]
    assert LLMRole.ASSISTANT in roles
    assert LLMRole.TOOL in roles
    tool_msg = next(m for m in second.messages if m.role == LLMRole.TOOL)
    assert tool_msg.tool_call_id == "call_1"
    assert "hello from main.py" in tool_msg.content
    assistant_msg = next(m for m in second.messages if m.role == LLMRole.ASSISTANT)
    assert assistant_msg.tool_calls and assistant_msg.tool_calls[0].id == "call_1"


@pytest.mark.asyncio
async def test_composer_image_attachment_is_hydrated_into_user_message():
    from devai.adapters.object_store import NoopObjectStoreAdapter

    store = NoopObjectStoreAdapter()
    await store.put("uploads/mock.png", b"\x89PNG\r\n\x1a\n", content_type="image/png")

    spec = Specialization(
        name="ui_role",
        handover_schema={"summary": HandoverField(name="summary", type="string", required=True)},
    )
    llm = FakeLLMAdapter([LLMResponse(text='```json\n{"summary": "saw the mock"}\n```')])
    deps = StageDeps(config=None, llm=llm, extra={"object_store": store})
    task = _task()
    task.agent_context["attachments"] = ["uploads/mock.png"]

    run = await AgentRunner(deps).run(spec, task)
    assert run.patch == {"summary": "saw the mock"}

    user_msg = llm.requests[0].messages[0]
    assert user_msg.role == LLMRole.USER
    assert len(user_msg.images) == 1
    assert user_msg.images[0]["media_type"] == "image/png"
    assert user_msg.images[0]["data"]  # base64 payload present


@pytest.mark.asyncio
async def test_no_llm_returns_stub():
    spec = Specialization(name="noop_role")
    run = await AgentRunner(_deps(None)).run(spec, _task())
    assert run.stub is True
    assert run.patch.get("reason") == "no_llm_adapter"


@pytest.mark.asyncio
async def test_missing_required_handover_field_flags_violation():
    spec = Specialization(
        name="strict_role",
        handover_schema={"pr_number": HandoverField(name="pr_number", type="integer", required=True)},
    )
    llm = FakeLLMAdapter([LLMResponse(text='```json\n{"note": "forgot the pr"}\n```')])
    run = await AgentRunner(_deps(llm)).run(spec, _task())
    assert "_handover_violations" in run.patch


@pytest.mark.asyncio
async def test_soft_error_response_fails_the_run():
    # Adapters return LLMResponse(finish_reason="error") instead of raising;
    # the loop must not report that as a clean empty answer.
    llm = FakeLLMAdapter(
        [LLMResponse(text="", finish_reason="error", extra={"status_code": 403, "error": "RBAC: access denied"})]
    )
    run = await AgentRunner(_deps(llm)).run(Specialization(name="summarizer"), _task())

    assert run.error.startswith("llm_error:")
    assert "RBAC: access denied" in run.error
    assert run.final_text == ""
