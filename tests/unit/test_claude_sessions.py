"""Session-chained Claude agent loop.

The contract: long agent work runs as bounded conversational sessions —
each session does a few tool turns, checkpoints "COMPLETED / REMAINING",
and the next session continues FRESH from the brief + note. Natural
completion (no tool calls) ends the chain; "REMAINING: none" at a
boundary ends it too; exhausting sessions returns the last note as a
partial result.
"""

from __future__ import annotations

import pytest

anthropic = pytest.importorskip("anthropic")

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from devai.providers.anthropic_claude import ClaudeProvider  # noqa: E402


class _Cfg:
    anthropic_api_key = "test-key"
    claude_model = "claude-sonnet-4-20250514"
    claude_max_tokens = 1024
    claude_max_iterations = 50
    claude_session_iterations = 2  # tiny sessions for the test
    claude_max_sessions = 3
    claude_prompt_caching = True
    claude_agent_total_timeout = 30


class _Resp:
    def __init__(self, content):
        self.content = content


def _text(t: str) -> TextBlock:
    return TextBlock(type="text", text=t)


def _tool(i: str) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=i, name="scm_commit_file", input={"path": f"f{i}.ts"})


def _provider(scripted: list[_Resp]) -> tuple[ClaudeProvider, list[list[dict]]]:
    p = ClaudeProvider(_Cfg())
    seen_messages: list[list[dict]] = []

    async def fake_call(system_prompt, tools, messages):
        seen_messages.append([m for m in messages])
        return scripted.pop(0)

    p._call_api = fake_call  # type: ignore[method-assign]
    return p, seen_messages


@pytest.mark.asyncio
async def test_session_boundary_checkpoints_and_next_session_continues():
    scripted = [
        _Resp([_tool("t1")]),  # s1 turn 1 — tool
        _Resp([_tool("t2")]),  # s1 turn 2 — boundary: tools NOT executed
        _Resp([_text("COMPLETED: configs\nREMAINING: pages + API")]),  # checkpoint note
        _Resp([_text("All done — PR #7 opened.")]),  # s2 turn 1 — natural completion
    ]
    p, seen = _provider(scripted)
    executed: list[str] = []

    async def tool_executor(name, inp):
        executed.append(inp.get("path", ""))
        return "ok"

    result = await p.run_agent_loop("sys", "build the app", [{"name": "scm_commit_file"}], tool_executor)

    assert result == "All done — PR #7 opened."
    assert executed == ["ft1.ts"]  # boundary turn's tool was checkpointed, not executed
    # Session 2 starts fresh with the brief + progress note
    s2_seed = seen[3][0]["content"][0]["text"]
    assert "PROGRESS FROM PREVIOUS SESSION" in s2_seed
    assert "REMAINING: pages + API" in s2_seed
    assert len(seen[3]) == 1  # fresh conversation, not the old thread


@pytest.mark.asyncio
async def test_remaining_none_ends_the_chain_at_a_boundary():
    scripted = [
        _Resp([_tool("t1")]),
        _Resp([_tool("t2")]),  # boundary
        _Resp([_text("COMPLETED: everything\nREMAINING: none")]),
    ]
    p, _ = _provider(scripted)

    async def tool_executor(name, inp):
        return "ok"

    result = await p.run_agent_loop("sys", "build", [{"name": "scm_commit_file"}], tool_executor)
    assert "REMAINING: none" in result


@pytest.mark.asyncio
async def test_exhausted_sessions_return_last_note_as_partial():
    # Every session burns its budget; notes always report remaining work.
    scripted = []
    for n in range(3):  # max_sessions = 3
        scripted += [
            _Resp([_tool(f"a{n}")]),
            _Resp([_tool(f"b{n}")]),
            _Resp([_text(f"COMPLETED: part {n}\nREMAINING: more")]),
        ]
    p, _ = _provider(scripted)

    async def tool_executor(name, inp):
        return "ok"

    result = await p.run_agent_loop("sys", "build", [{"name": "scm_commit_file"}], tool_executor)
    assert "COMPLETED: part 2" in result  # last checkpoint, not an exception


@pytest.mark.asyncio
async def test_turn_envelopes_are_emitted_with_usage_and_tools():
    from devai.services import agent_turns

    captured: list[tuple[str, dict]] = []

    async def sink(run_id, envelope):
        captured.append((run_id, envelope))

    token = agent_turns.set_turn_context("devai-test123", "senior_developer", "implement-code")
    agent_turns.set_turn_sink(sink)
    try:
        scripted = [
            _Resp([_text("Creating the config."), _tool("t1")]),
            _Resp([_tool("t2")]),  # boundary
            _Resp([_text("COMPLETED: x\nREMAINING: none")]),
        ]
        p, _ = _provider(scripted)

        async def tool_executor(name, inp):
            return "ok"

        await p.run_agent_loop("sys", "build", [{"name": "scm_commit_file"}], tool_executor)
    finally:
        agent_turns.set_turn_sink(None)
        agent_turns.reset_turn_context(token)

    kinds = [e["kind"] for _, e in captured]
    assert kinds[0] == "agent_start"
    assert "turn" in kinds and "checkpoint" in kinds and kinds[-1] == "agent_done"
    turn = next(e for _, e in captured if e["kind"] == "turn")
    assert turn["agent"] == "senior_developer"
    assert turn["stage"] == "implement-code"
    assert turn["tools"][0]["name"] == "scm_commit_file"
    assert all(rid == "devai-test123" for rid, _ in captured)


@pytest.mark.asyncio
async def test_cache_breakpoint_moves_to_newest_user_turn():
    p = ClaudeProvider(_Cfg())
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "brief", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": [_text("thinking")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    system_param, tools_param = p._cache_params("sys", [{"name": "a"}, {"name": "b"}], messages)

    assert system_param[0]["cache_control"] == {"type": "ephemeral"}
    assert tools_param[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[0]["content"][0]  # old marker stripped
    assert messages[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}  # moved
