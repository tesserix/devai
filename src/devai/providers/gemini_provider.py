"""Google Gemini provider for fast inference.

Used for document analysis, tech detection, and other tasks
where Gemini's speed and multimodal capabilities are beneficial.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import google.generativeai as genai

from devai.providers.groq_provider import _fetch_secret_via_adapter

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Google Gemini provider for LLM inference."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        api_key = config.gemini_api_key
        if not api_key:
            api_key = self._fetch_from_gcp(config.gcp_secret_gemini_api_key)

        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(config.gemini_model)
        self._model_name = config.gemini_model

    def _fetch_from_gcp(self, secret_name: str) -> str:
        """Fetch the API key via the secrets adapter (GCP SM SDK + Workload Identity).

        Routes through ``adapters/secrets/factory`` instead of shelling out to the
        ``gcloud`` CLI, which is absent from the distroless image. The project comes
        from settings (``DEVAI_SECRETS_GCP_PROJECT`` / ``DEVAI_GKE_PROJECT``).
        """
        value = _fetch_secret_via_adapter(self._config, secret_name)
        if value:
            logger.info("Loaded Gemini API key from secrets adapter: %s", secret_name)
        return value

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using Google Gemini."""
        import asyncio

        # A user whose LLM connector pins a non-Gemini provider gets THEIR
        # provider, not the platform Gemini key — the connector choice wins
        # over this agent's hardcoded preference.
        overlaid = set(getattr(self._config, "overlaid_attrs", ()) or ())
        pinned = str(getattr(self._config, "llm_provider", "") or "").lower()
        if (
            overlaid
            and "gemini_api_key" not in overlaid
            and "llm_provider" in overlaid
            and pinned not in ("", "gemini", "vertex_gemini")
        ):
            from devai.providers.anthropic_claude import ClaudeProvider

            claude = ClaudeProvider(self._config)
            return await claude.generate(
                system_prompt=system or "You are a helpful assistant.",
                user_message=prompt,
            )

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        try:
            response = await asyncio.to_thread(
                self.model.generate_content,
                full_prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            return response.text or ""
        except Exception as e:
            # Fall back to OpenAI if available
            if self._config.openai_api_key:
                logger.warning("Gemini failed (%s), falling back to OpenAI", e)
                return await self._fallback_to_openai(prompt, system, max_tokens)
            raise

    async def _fallback_to_openai(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
    ) -> str:
        """Fall back to OpenAI when Gemini is unavailable."""
        from devai.providers.openai_provider import OpenAIProvider

        openai = OpenAIProvider(self._config)
        return await openai.generate(prompt=prompt, system=system, max_tokens=max_tokens)
