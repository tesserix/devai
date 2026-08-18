"""Google Gemini provider for fast inference.

Used for document analysis, tech detection, and other tasks
where Gemini's speed and multimodal capabilities are beneficial.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types

from devai.providers.groq_provider import _fetch_secret_via_adapter

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

    from devai.config import Settings

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Google Gemini provider for LLM inference."""

    def __init__(self, config: Settings) -> None:
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required

        self._config = config
        api_key = config.gemini_api_key
        if not api_key:
            api_key = self._fetch_from_gcp(config.gcp_secret_gemini_api_key)

        self._gateway_client = None
        self._client: genai.Client | None = None
        if gateway_required(config):
            from openai import AsyncOpenAI

            self._gateway_client = AsyncOpenAI(
                api_key=api_key,
                base_url=gateway_base_url(config, "gemini"),
                default_headers={"x-devai-provider": "gemini"},
            )
        else:
            self._client = genai.Client(api_key=api_key)
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
            return str(
                await claude.generate(
                    system_prompt=system or "You are a helpful assistant.",
                    user_message=prompt,
                )
            )

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        try:
            if self._gateway_client is not None:
                messages: list[ChatCompletionMessageParam] = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})
                gateway_response = await self._gateway_client.chat.completions.create(
                    model=self._model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return gateway_response.choices[0].message.content or ""
            if self._client is None:
                raise RuntimeError("Gemini client is not configured")
            gemini_response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            return gemini_response.text or ""
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
