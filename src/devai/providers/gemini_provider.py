"""Google Gemini provider for fast inference.

Used for document analysis, tech detection, and other tasks
where Gemini's speed and multimodal capabilities are beneficial.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import google.generativeai as genai

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
        """Fetch API key from GCP Secret Manager."""
        try:
            import subprocess

            result = subprocess.run(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "access",
                    "latest",
                    f"--secret={secret_name}",
                    "--project=tesseracthub-480811",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info("Loaded Gemini API key from GCP Secret Manager: %s", secret_name)
                return result.stdout.strip()
        except Exception as e:
            logger.debug("Could not fetch GCP secret %s: %s", secret_name, e)
        return ""

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
