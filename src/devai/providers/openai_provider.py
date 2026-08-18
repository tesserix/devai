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
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required

        self._config = config
        self.client = AsyncOpenAI(
            api_key=config.openai_api_key,
            base_url=gateway_base_url(config, "openai", getattr(config, "openai_base_url", "")),
            default_headers={"x-devai-provider": "openai"} if gateway_required(config) else None,
        )
        self.model = config.openai_model

    def _user_pinned_elsewhere(self) -> bool:
        """True when this run's config is a USER's settings overlay whose
        LLM connector pins a non-OpenAI provider (and they brought no
        OpenAI key). Their connector choice wins over this agent's
        hardcoded provider preference — never dial the platform OpenAI
        key for a user who set up their own LLM."""
        overlaid = set(getattr(self._config, "overlaid_attrs", ()) or ())
        if not overlaid or "openai_api_key" in overlaid:
            return False
        provider = str(getattr(self._config, "llm_provider", "") or "").lower()
        return "llm_provider" in overlaid and provider not in ("", "openai")

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using OpenAI Chat Completions."""
        if self._user_pinned_elsewhere():
            return await self._fallback_to_claude(prompt, system, max_tokens)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # o3/o4 models use max_completion_tokens instead of max_tokens
        token_param = "max_completion_tokens" if self.model.startswith(("o1", "o3", "o4")) else "max_tokens"
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            token_param: max_tokens,
        }
        # o-series models don't support temperature
        if not self.model.startswith(("o1", "o3", "o4")):
            kwargs["temperature"] = temperature
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
