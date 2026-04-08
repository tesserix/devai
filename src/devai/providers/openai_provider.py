"""OpenAI Chat Completions provider for general-purpose agents.

Used as the default provider for agents that need reliable, high-quality
generation without tool-use loops (those use Claude).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """OpenAI Chat Completions provider."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self.client = AsyncOpenAI(api_key=config.openai_api_key)
        self.model = config.openai_model

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using OpenAI Chat Completions."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as e:
            # Fall back to Anthropic if available
            if self._config.anthropic_api_key:
                logger.warning("OpenAI failed (%s), falling back to Claude", e)
                return await self._fallback_to_claude(prompt, system, max_tokens)
            raise

    async def _fallback_to_claude(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
    ) -> str:
        """Fall back to Anthropic Claude when OpenAI is unavailable."""
        from devai.providers.anthropic_claude import ClaudeProvider

        claude = ClaudeProvider(self._config)
        return await claude.generate(
            system_prompt=system or "You are a helpful assistant.",
            user_message=prompt,
        )
