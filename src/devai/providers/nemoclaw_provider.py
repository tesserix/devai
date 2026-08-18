"""NemoClaw / Nemotron provider for GPU-accelerated inference.

Connects to a self-hosted NemoClaw instance running Nemotron 3 Super 120B
or Qwen2.5-Coder-32B via an OpenAI-compatible API endpoint (vLLM / NIM / Ollama).

Supports both simple generation and agentic tool-calling loops.
Falls back to Groq cloud if the local GPU endpoint is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from devai.services.resilience import CircuitBreaker, retry_async, with_timeout
from devai.services.tracing import wrap_openai_client

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Coroutine[Any, Any, str]]

# Circuit breaker for local GPU inference
_nemoclaw_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60, name="nemoclaw")

# Timeouts
AGENT_TIMEOUT = 900  # 15 min for full agentic loop
API_CALL_TIMEOUT = 300  # 5 min per call (large models are slower)


class NemoClawProvider:
    """NemoClaw GPU inference provider (OpenAI-compatible API)."""

    def __init__(self, config: Settings) -> None:
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required

        api_key = config.nemoclaw_api_key or "not-needed"
        base_url = config.nemoclaw_endpoint

        if not base_url:
            base_url = self._discover_endpoint()
        base_url = gateway_base_url(config, "nemoclaw", base_url)

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"x-devai-provider": "nemoclaw"} if gateway_required(config) else None,
        )
        self.client = wrap_openai_client(client)
        self.model = config.nemoclaw_model
        self.max_tokens = config.nemoclaw_max_tokens
        self.max_iterations = config.nemoclaw_max_iterations
        self._fallback_enabled = config.nemoclaw_fallback_to_groq

    def _discover_endpoint(self) -> str:
        """Try K8s service discovery for NemoClaw inference."""
        # In-cluster: NemoClaw NIM runs as a K8s service
        endpoints = [
            "http://nemoclaw-inference.devai.svc.cluster.local:8000/v1",
            "http://nemoclaw-inference:8000/v1",
            "http://localhost:8000/v1",
        ]
        # Default to first — health check will verify at call time
        logger.info("NemoClaw endpoint not configured, using K8s service discovery")
        return endpoints[0]

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using the local GPU model."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = await self._call_api_generate(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as e:
            if self._fallback_enabled:
                logger.warning("NemoClaw unavailable (%s), falling back to Groq", e)
                return await self._fallback_generate(prompt, system, temperature, max_tokens)
            raise

    async def run_agent_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        max_iterations: int | None = None,
    ) -> str:
        """Run a tool-calling agentic loop on the local GPU model.

        Uses OpenAI-compatible function calling (supported by vLLM and NIM).
        Mirrors the Claude provider's graceful wrap-up: when the iteration
        ceiling is hit, the loop refuses further tool calls and asks the
        model to summarize, returning whatever text it produces instead of
        raising — so a runaway agent doesn't fail the whole pipeline.
        """

        async def _run() -> str:
            iterations = max_iterations or self.max_iterations
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # Convert Anthropic-style tools to OpenAI function format
            openai_tools = self._convert_tools(tools)
            last_text = ""

            for i in range(iterations):
                try:
                    response = await self._call_api_chat(messages, openai_tools)
                except Exception as e:
                    if self._fallback_enabled:
                        logger.warning("NemoClaw loop failed at iteration %d (%s), cannot fallback mid-loop", i, e)
                    raise

                choice = response.choices[0]
                messages.append(choice.message.model_dump())

                if choice.message.content:
                    last_text = choice.message.content

                # No tool calls — agent is done
                if not choice.message.tool_calls:
                    logger.info("NemoClaw agent completed in %d iterations", i + 1)
                    return choice.message.content or last_text

                # Last allowed iteration — refuse new tool calls and ask
                # the model to wrap up with what it has.
                if i == iterations - 1:
                    logger.warning(
                        "NemoClaw agent reached iteration cap (%d) — requesting wrap-up",
                        iterations,
                    )
                    for tc in choice.message.tool_calls:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": (
                                    "ITERATION LIMIT REACHED. Do NOT call any more tools. "
                                    "Summarize what you have accomplished, list remaining "
                                    "work as TODOs in your final message, and stop."
                                ),
                            }
                        )

                    try:
                        final = await self._call_api_chat(messages, openai_tools)
                        final_text = final.choices[0].message.content or ""
                    except Exception as e:
                        logger.warning("NemoClaw wrap-up call failed: %s", e)
                        final_text = ""

                    if final_text.strip():
                        return final_text
                    if last_text:
                        return last_text
                    raise RuntimeError(f"NemoClaw agent exceeded {iterations} iterations and produced no text")

                # Execute tool calls
                for tc in choice.message.tool_calls:
                    func_name = tc.function.name
                    import json

                    func_args = json.loads(tc.function.arguments)

                    logger.debug("Executing tool: %s(%s)", func_name, func_args)
                    try:
                        result = await with_timeout(
                            tool_executor(func_name, func_args),
                            timeout_seconds=60,
                            description=f"tool:{func_name}",
                        )
                    except Exception as e:
                        logger.error("Tool %s failed: %s", func_name, e)
                        result = f"Error: {e}"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        }
                    )

            return last_text

        return await with_timeout(_run(), AGENT_TIMEOUT, "nemoclaw_agent_loop")

    @retry_async(max_attempts=2, base_delay=3.0, max_delay=30.0)
    async def _call_api_generate(self, **kwargs: Any) -> Any:
        """Single generation call with retry and circuit breaker."""

        async def _call() -> Any:
            return await asyncio.wait_for(
                self.client.chat.completions.create(**kwargs),
                timeout=API_CALL_TIMEOUT,
            )

        return await _nemoclaw_breaker.call(_call)

    @retry_async(max_attempts=2, base_delay=3.0, max_delay=30.0)
    async def _call_api_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        """Chat completion with tools, retry, and circuit breaker."""

        async def _call() -> Any:
            return await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools if tools else None,
                    max_tokens=self.max_tokens,
                    temperature=0.2,
                ),
                timeout=API_CALL_TIMEOUT,
            )

        return await _nemoclaw_breaker.call(_call)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Anthropic-style tool definitions to OpenAI function format."""
        openai_tools = []
        for tool in tools:
            if "input_schema" in tool:
                # Anthropic format → OpenAI format
                openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool["input_schema"],
                        },
                    }
                )
            elif "function" in tool:
                # Already OpenAI format
                openai_tools.append(tool)
            else:
                openai_tools.append(
                    {
                        "type": "function",
                        "function": tool,
                    }
                )
        return openai_tools

    async def _fallback_generate(
        self,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        """Fall back to Groq cloud when local GPU is unavailable."""
        from devai.config import settings
        from devai.providers.groq_provider import GroqProvider

        groq = GroqProvider(settings)
        return await groq.generate(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
        )

    async def health_check(self) -> dict[str, Any]:
        """Check if the local GPU inference endpoint is healthy."""
        try:
            models = await asyncio.wait_for(
                self.client.models.list(),
                timeout=10,
            )
            model_ids = [m.id for m in models.data]
            return {
                "status": "healthy",
                "endpoint": str(self.client.base_url),
                "models": model_ids,
                "active_model": self.model,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "endpoint": str(self.client.base_url),
                "error": str(e),
            }
