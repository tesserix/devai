"""Claude Messages API provider with tool-use agentic loop.

The loop runs as a CHAIN OF BOUNDED CONVERSATIONAL SESSIONS rather than
one ever-growing thread:

  - each session does at most `claude_session_iterations` tool turns;
  - at the session boundary the model writes a checkpoint note
    ("completed / remaining TODOs") and the next session starts FRESH
    from the original brief + that note;
  - the chain ends when the model finishes naturally (a turn with no
    tool calls) or the session/iteration budget is exhausted — in which
    case the last checkpoint note is returned as a partial result (the
    stage output contracts + recovery agent take it from there).

Why sessions: a single long conversation re-sends its whole history on
every turn — input tokens grow linearly, the shared rate limiter starts
sleeping ~60s per call, and a 100-turn build needs hours it doesn't
have. Short sessions keep every turn cheap and make progress durable:
losing a session loses minutes, not the whole build.

Prompt caching (`claude_prompt_caching`) adds cache_control breakpoints
on the system prompt, tool schemas, and the newest user turn so each
turn re-reads the conversation prefix from cache instead of re-paying
full input tokens. The rate limiter is fed the per-turn DELTA estimate
accordingly.

Includes retry with exponential backoff, timeout protection, and
LangSmith tracing integration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock

from devai.services.agent_turns import emit_turn
from devai.services.resilience import (
    CircuitBreaker,
    estimate_token_count,
    get_claude_rate_limiter,
    retry_async,
    with_timeout,
)

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.config import Settings

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Coroutine[Any, Any, str]]

# Circuit breaker for Anthropic API
_claude_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=120, name="anthropic")

# Single API call timeout (3 minutes)
API_CALL_TIMEOUT = 180
DEFAULT_TOOL_TIMEOUT = 300


def _compact_input(tool_input: Any) -> str:
    """One-line summary of a tool call's input for the turn log — the path/
    branch/title style fields humans scan for, never full file contents."""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:120]
    for key in ("path", "branch_name", "branch", "title", "issue_id", "pr_id", "repo"):
        if tool_input.get(key):
            return f"{key}={str(tool_input[key])[:100]}"
    return ", ".join(list(tool_input.keys())[:5])[:120]


_CHECKPOINT_INSTRUCTION = (
    "SESSION CHECKPOINT — do NOT call any more tools in this session. "
    "Reply with exactly two sections:\n"
    "COMPLETED: what you finished this session (files, branches, PRs — be precise).\n"
    "REMAINING: the precise remaining TODOs, in order, so the next session "
    "can continue without re-reading everything.\n"
    "If ALL work is done, say COMPLETED: <summary> and REMAINING: none."
)


class ClaudeProvider:
    """Anthropic Claude Messages API with a session-chained tool-use loop."""

    def __init__(self, config: Settings, model: str | None = None) -> None:
        """``model`` overrides the global default per role — the dev agent
        runs the UI model for UI work and claude-opus-4-8 for API work,
        reviewers run the review model, etc. (config llm_model_* fields)."""
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required
        from devai.adapters.llm.legacy_bridge import current_legacy_llm
        from devai.services.tracing import wrap_anthropic_client

        self._gateway_required = gateway_required(config)
        self._resolved_llm: LLMAdapter | None = current_legacy_llm()
        self.model = model or config.claude_model
        self.max_tokens = config.claude_max_tokens
        self.max_iterations = config.claude_max_iterations
        self.session_iterations = max(1, int(getattr(config, "claude_session_iterations", 15) or 15))
        self.max_sessions = max(1, int(getattr(config, "claude_max_sessions", 8) or 8))
        self.prompt_caching = bool(getattr(config, "claude_prompt_caching", True))
        self.total_timeout = int(getattr(config, "claude_agent_total_timeout", 14400) or 14400)
        if self._resolved_llm is not None:
            self.client = None
            return
        self.client = wrap_anthropic_client(
            AsyncAnthropic(
                api_key=config.anthropic_api_key,
                base_url=gateway_base_url(config, "anthropic", getattr(config, "anthropic_base_url", "")),
                default_headers={"x-devai-provider": "anthropic"} if gateway_required(config) else None,
            )
        )

    async def run_agent_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        max_iterations: int | None = None,
    ) -> str:
        """Run the session-chained agent loop until completion.

        Degrades gracefully on every budget edge: a session that hits its
        turn cap checkpoints and hands over to the next session; exhausting
        all sessions returns the last checkpoint note (partial result)
        instead of raising. Only a loop that produced literally no text
        raises.
        """
        # ADK security: prepend the standing trusted/untrusted directive to every
        # acting agent's system prompt — the model-facing half of the injection
        # defense (the enforcement half is the tool-policy + output validation).
        from devai.services.prompt_guard import SECURITY_DIRECTIVE

        system_prompt = f"{SECURITY_DIRECTIVE}\n\n{system_prompt}"

        async def _run() -> str:
            total_cap = max_iterations or self.max_iterations
            session_cap = max(1, min(self.session_iterations, total_cap))
            turns_used = 0
            progress_notes: list[str] = []
            last_text = ""

            await emit_turn(
                "agent_start",
                model=self.model,
                max_turns=total_cap,
                session_turns=session_cap,
                max_sessions=self.max_sessions,
            )

            for session_no in range(1, self.max_sessions + 1):
                if turns_used >= total_cap:
                    break

                seed = user_message
                if progress_notes:
                    notes = "\n\n".join(progress_notes[-2:])
                    seed = (
                        f"{user_message}\n\n"
                        "## PROGRESS FROM PREVIOUS SESSION(S) — CONTINUE, DO NOT REDO\n"
                        f"{notes}\n\n"
                        "The work above is already done and committed. Verify with read "
                        "tools if unsure, then complete ONLY the remaining work."
                    )
                messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": seed}]}]

                for i in range(session_cap):
                    turns_used += 1
                    response = await self._call_api(system_prompt, tools, messages)
                    assistant_content = response.content
                    messages.append({"role": "assistant", "content": assistant_content})

                    turn_text = "\n".join(block.text for block in assistant_content if isinstance(block, TextBlock))
                    if turn_text.strip():
                        last_text = turn_text

                    tool_calls = [b for b in assistant_content if isinstance(b, ToolUseBlock)]
                    usage = getattr(response, "usage", None)
                    await emit_turn(
                        "turn",
                        turn=turns_used,
                        session=session_no,
                        model=self.model,
                        usage_in=getattr(usage, "input_tokens", 0) or 0,
                        usage_out=getattr(usage, "output_tokens", 0) or 0,
                        cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                        text=turn_text.strip()[:400],
                        tools=[{"name": tc.name, "input": _compact_input(tc.input)} for tc in tool_calls],
                    )
                    if not tool_calls:
                        logger.info(
                            "Claude agent completed naturally — session %d, %d total turns",
                            session_no,
                            turns_used,
                        )
                        await emit_turn("agent_done", reason="natural", turns_used=turns_used)
                        return turn_text or last_text

                    # Session boundary (or global cap): checkpoint instead of
                    # executing more tools — the next session continues from
                    # the note with a fresh, small context.
                    if i == session_cap - 1 or turns_used >= total_cap:
                        logger.info(
                            "Claude agent session %d hit its turn budget (%d) — checkpointing",
                            session_no,
                            session_cap,
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tc.id,
                                        "content": _CHECKPOINT_INSTRUCTION,
                                        "is_error": True,
                                    }
                                    for tc in tool_calls
                                ],
                            }
                        )
                        note = await self._call_api(system_prompt, tools, messages)
                        note_text = "\n".join(b.text for b in note.content if isinstance(b, TextBlock))
                        if note_text.strip():
                            progress_notes.append(note_text.strip())
                            last_text = note_text
                            await emit_turn(
                                "checkpoint",
                                session=session_no,
                                turn=turns_used,
                                text=note_text.strip()[:600],
                            )
                            # The model itself declares completion at a boundary.
                            if "remaining: none" in note_text.lower():
                                logger.info(
                                    "Claude agent declared completion at session %d boundary",
                                    session_no,
                                )
                                await emit_turn("agent_done", reason="remaining_none", turns_used=turns_used)
                                return note_text
                        break  # next session

                    tool_results = []
                    for tc in tool_calls:
                        logger.debug("Executing tool: %s(%s)", tc.name, tc.input)
                        try:
                            timeout_seconds = DEFAULT_TOOL_TIMEOUT
                            if tc.name.startswith(("github_", "scm_", "doc_")):
                                timeout_seconds = 120
                            result = await with_timeout(
                                tool_executor(tc.name, tc.input),
                                timeout_seconds=timeout_seconds,
                                description=f"tool:{tc.name}",
                            )
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tc.id,
                                    "content": result,
                                }
                            )
                        except Exception as e:
                            logger.error("Tool %s failed: %s", tc.name, e)
                            await emit_turn(
                                "tool_result",
                                turn=turns_used,
                                tool=tc.name,
                                error=str(e)[:300],
                            )
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tc.id,
                                    "content": f"Error: {e}",
                                    "is_error": True,
                                }
                            )

                    messages.append({"role": "user", "content": tool_results})

            if last_text:
                logger.warning(
                    "Claude agent exhausted its session budget (%d sessions, %d turns) — "
                    "returning last checkpoint as partial result",
                    self.max_sessions,
                    turns_used,
                )
                await emit_turn("agent_done", reason="budget_exhausted", turns_used=turns_used)
                return last_text
            raise RuntimeError(
                f"Claude agent produced no text in {turns_used} turns across {self.max_sessions} session(s)"
            )

        return await with_timeout(_run(), self.total_timeout, "claude_agent_loop")

    # ── Prompt caching ──────────────────────────────────────────────────

    def _cache_params(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Apply cache_control breakpoints (system, tools, newest user turn).

        The conversation breakpoint MOVES each call: strip markers from the
        user-turn dicts we own (assistant turns hold SDK objects — never
        touched), then mark the last block of the newest user message so the
        whole prefix above it is a cache hit on the next turn.
        """
        if not self.prompt_caching:
            return system_prompt, tools

        system_param = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        tools_param = tools
        if tools:
            tools_param = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]

        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        for m in reversed(messages):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1]["cache_control"] = {"type": "ephemeral"}
            break

        return system_param, tools_param

    @retry_async(max_attempts=3, base_delay=2.0, max_delay=30.0)
    async def _call_api(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Message:
        """Make a single API call with proactive rate limiting + retry.

        The rate limiter is the first line of defense — it sleeps before
        the call to make sure we don't blow the per-minute budget. The
        retry decorator is the second line, catching any 429 that slips
        through (e.g. concurrent calls from a different process) and
        waiting the API-suggested retry-after.
        """
        resolved_llm: LLMAdapter | None = getattr(self, "_resolved_llm", None)
        if resolved_llm is not None:
            return await self._call_resolved_llm(system_prompt, tools, messages)

        # Budget estimate: with prompt caching only the NEW tokens since the
        # last turn are real spend — feeding the limiter the full (growing)
        # history made it sleep a whole window before every late turn, which
        # throttled implement loops to ~1 tool turn per minute.
        if self.prompt_caching and len(messages) > 1:
            estimated_input_tokens = estimate_token_count(messages[-1]) + 2000
        else:
            estimated_input_tokens = (
                estimate_token_count(system_prompt) + estimate_token_count(tools) + estimate_token_count(messages)
            )
        await get_claude_rate_limiter().acquire(estimated_input_tokens)

        system_param, tools_param = self._cache_params(system_prompt, tools, messages)
        extra_headers = None
        if self._gateway_required:
            from devai.adapters.llm.gateway_routing import current_gateway_headers

            extra_headers = current_gateway_headers("anthropic")

        async def _api_call() -> Message:
            return await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_param,
                    tools=tools_param,
                    messages=messages,
                    extra_headers=extra_headers,
                ),
                timeout=API_CALL_TIMEOUT,
            )

        return await _claude_breaker.call(_api_call)

    async def _call_resolved_llm(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Any:
        from types import SimpleNamespace

        from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole, ToolCall, ToolSpec

        canonical: list[LLMMessage] = []
        tool_names: dict[str, str] = {}
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content", "")
            if role == "assistant":
                text_parts: list[str] = []
                calls: list[ToolCall] = []
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
                        tool_names[block.id] = block.name
                canonical.append(LLMMessage(role=LLMRole.ASSISTANT, content="\n".join(text_parts), tool_calls=calls))
                continue
            if isinstance(content, str):
                canonical.append(LLMMessage(role=LLMRole.USER, content=content))
                continue
            text_parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if text_parts:
                canonical.append(LLMMessage(role=LLMRole.USER, content="\n".join(text_parts)))
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_call_id = str(block.get("tool_use_id", ""))
                canonical.append(
                    LLMMessage(
                        role=LLMRole.TOOL,
                        content=str(block.get("content", "")),
                        name=tool_names.get(tool_call_id, ""),
                        tool_call_id=tool_call_id,
                    )
                )

        resolved_llm: LLMAdapter | None = getattr(self, "_resolved_llm", None)
        if resolved_llm is None:
            raise RuntimeError("resolved LLM is not initialized")
        response = await resolved_llm.generate(
            LLMRequest(
                system=system_prompt,
                messages=canonical,
                tools=[
                    ToolSpec(
                        name=str(tool.get("name", "")),
                        description=str(tool.get("description", "")),
                        parameters=dict(tool.get("input_schema") or {}),
                    )
                    for tool in tools
                ],
                max_tokens=self.max_tokens,
            )
        )
        if response.finish_reason == "error":
            raise RuntimeError("all authorized LLM providers failed")

        content_blocks: list[Any] = []
        if response.text:
            content_blocks.append(TextBlock(type="text", text=response.text, citations=None))
        content_blocks.extend(
            ToolUseBlock(type="tool_use", id=call.id, name=call.name, input=dict(call.arguments))
            for call in response.tool_calls
        )
        usage = SimpleNamespace(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            cache_read_input_tokens=response.usage.cached_tokens,
            cache_creation_input_tokens=0,
        )
        return SimpleNamespace(content=content_blocks, usage=usage)

    @retry_async(max_attempts=3, base_delay=1.0)
    async def generate(
        self,
        system_prompt: str,
        user_message: str,
    ) -> str:
        """Simple one-shot generation without tools."""
        resolved_llm: LLMAdapter | None = getattr(self, "_resolved_llm", None)
        if resolved_llm is not None:
            from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

            resolved_response = await resolved_llm.generate(
                LLMRequest(
                    system=system_prompt,
                    messages=[LLMMessage(role=LLMRole.USER, content=user_message)],
                    max_tokens=self.max_tokens,
                )
            )
            if resolved_response.finish_reason == "error":
                raise RuntimeError("all authorized LLM providers failed")
            return resolved_response.text
        # Same proactive throttle as the agent-loop path.
        estimated_input_tokens = estimate_token_count(system_prompt) + estimate_token_count(user_message)
        await get_claude_rate_limiter().acquire(estimated_input_tokens)
        extra_headers = None
        if self._gateway_required:
            from devai.adapters.llm.gateway_routing import current_gateway_headers

            extra_headers = current_gateway_headers("anthropic")

        client = self.client
        if client is None:
            raise RuntimeError("Anthropic client is not initialized")
        response = await asyncio.wait_for(
            client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                extra_headers=extra_headers,
            ),
            timeout=API_CALL_TIMEOUT,
        )
        text_parts = [block.text for block in response.content if isinstance(block, TextBlock)]
        return "\n".join(text_parts)
