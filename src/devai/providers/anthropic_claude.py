"""Claude Messages API provider with tool-use agentic loop.

Includes retry with exponential backoff, timeout protection,
and LangSmith tracing integration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock

from devai.services.resilience import CircuitBreaker, retry_async, with_timeout

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Coroutine[Any, Any, str]]

# Circuit breaker for Anthropic API
_claude_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=120, name="anthropic")

# Agent execution timeout (15 minutes)
AGENT_TIMEOUT = 900
# Single API call timeout (3 minutes)
API_CALL_TIMEOUT = 180
DEFAULT_TOOL_TIMEOUT = 300


class ClaudeProvider:
    """Anthropic Claude Messages API with tool-use agentic loop."""

    def __init__(self, config: Settings) -> None:
        from devai.services.tracing import wrap_anthropic_client

        self.client = wrap_anthropic_client(AsyncAnthropic(api_key=config.anthropic_api_key))
        self.model = config.claude_model
        self.max_tokens = config.claude_max_tokens
        self.max_iterations = config.claude_max_iterations

    async def run_agent_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        max_iterations: int | None = None,
    ) -> str:
        """Run a Claude agent loop with tool use until completion.

        Includes timeout protection and retry logic for API calls. When the
        iteration ceiling is reached the loop degrades gracefully:

        1. Sends a final wrap-up turn telling the model to stop calling
           tools and produce its best final answer with what it has so far.
        2. Returns whatever text the model produces (even if partial).
        3. Only raises if there is literally no text to return.

        This prevents one runaway agent from failing the whole pipeline
        run, which is the right default for a development AI.
        """

        async def _run() -> str:
            iterations = max_iterations or self.max_iterations
            messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
            last_text = ""

            for i in range(iterations):
                response = await self._call_api(system_prompt, tools, messages)
                assistant_content = response.content
                messages.append({"role": "assistant", "content": assistant_content})

                # Capture any text the model produced this turn so we always
                # have something to return at the end, even on a forced stop.
                turn_text = "\n".join(block.text for block in assistant_content if isinstance(block, TextBlock))
                if turn_text.strip():
                    last_text = turn_text

                tool_calls = [block for block in assistant_content if isinstance(block, ToolUseBlock)]

                if not tool_calls:
                    logger.info("Claude agent completed in %d iterations", i + 1)
                    return turn_text or last_text

                # On the very last allowed iteration, refuse to execute any
                # more tool calls and give the model one final turn with a
                # stop instruction. This converts a hard cap into a soft
                # one without losing any reasoning the model has done.
                if i == iterations - 1:
                    logger.warning(
                        "Claude agent reached iteration cap (%d) — requesting wrap-up",
                        iterations,
                    )
                    tool_results = [
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": (
                                "ITERATION LIMIT REACHED. Do NOT call any more tools. "
                                "Summarize what you have already accomplished, list any "
                                "remaining work as TODOs in your final message, and stop."
                            ),
                            "is_error": True,
                        }
                        for tc in tool_calls
                    ]
                    messages.append({"role": "user", "content": tool_results})

                    final = await self._call_api(system_prompt, tools, messages)
                    final_text = "\n".join(block.text for block in final.content if isinstance(block, TextBlock))
                    if final_text.strip():
                        logger.warning("Claude agent wrapped up after iteration cap; returning partial result")
                        return final_text
                    if last_text:
                        return last_text
                    raise RuntimeError(f"Claude agent exceeded {iterations} iterations and produced no text")

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
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": f"Error: {e}",
                                "is_error": True,
                            }
                        )

                messages.append({"role": "user", "content": tool_results})

            # Defensive — the wrap-up branch above always returns or raises.
            return last_text

        return await with_timeout(_run(), AGENT_TIMEOUT, "claude_agent_loop")

    @retry_async(max_attempts=3, base_delay=2.0, max_delay=30.0)
    async def _call_api(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Message:
        """Make a single API call with retry and circuit breaker."""

        async def _api_call() -> Message:
            return await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                ),
                timeout=API_CALL_TIMEOUT,
            )

        return await _claude_breaker.call(_api_call)

    @retry_async(max_attempts=3, base_delay=1.0)
    async def generate(
        self,
        system_prompt: str,
        user_message: str,
    ) -> str:
        """Simple one-shot generation without tools."""
        response = await asyncio.wait_for(
            self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ),
            timeout=API_CALL_TIMEOUT,
        )
        text_parts = [block.text for block in response.content if isinstance(block, TextBlock)]
        return "\n".join(text_parts)
