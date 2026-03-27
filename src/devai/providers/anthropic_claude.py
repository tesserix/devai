"""Claude Messages API provider with tool-use agentic loop."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock, ToolUseBlock

from devai.config import Settings

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Coroutine[Any, Any, str]]


class ClaudeProvider:
    """Anthropic Claude Messages API with tool-use agentic loop."""

    def __init__(self, config: Settings) -> None:
        self.client = AsyncAnthropic(api_key=config.anthropic_api_key)
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

        The agent iterates: Claude generates a response, if it contains tool calls,
        we execute them and feed results back. Continues until Claude responds with
        only text (no tool calls) or we hit max iterations.

        Returns the final text output from Claude.
        """
        iterations = max_iterations or self.max_iterations
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        for i in range(iterations):
            response: Message = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # Extract tool calls
            tool_calls = [block for block in assistant_content if isinstance(block, ToolUseBlock)]

            if not tool_calls:
                # No tool calls — agent is done, extract final text
                text_parts = [block.text for block in assistant_content if isinstance(block, TextBlock)]
                final_text = "\n".join(text_parts)
                logger.info("Claude agent completed in %d iterations", i + 1)
                return final_text

            # Execute each tool call and collect results
            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                logger.debug("Executing tool: %s(%s)", tc.name, tc.input)
                try:
                    result = await tool_executor(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })
                except Exception as e:
                    logger.error("Tool %s failed: %s", tc.name, e)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    })

            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(f"Claude agent exceeded {iterations} iterations without completing")

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
    ) -> str:
        """Simple one-shot generation without tools."""
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text_parts = [block.text for block in response.content if isinstance(block, TextBlock)]
        return "\n".join(text_parts)
