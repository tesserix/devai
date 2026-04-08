"""Groq provider for fast inference (Llama 3.3 70B).

Used for lightweight tasks like requirements analysis and CI log parsing
where speed matters more than deep reasoning.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from groq import AsyncGroq

from devai.services.tracing import wrap_openai_client

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class GroqProvider:
    """Groq Cloud provider for fast LLM inference."""

    def __init__(self, config: Settings) -> None:
        api_key = config.groq_api_key
        if not api_key:
            api_key = self._fetch_from_gcp(config.gcp_secret_groq_api_key)

        client = AsyncGroq(api_key=api_key)
        # Wrap for LangSmith tracing (Groq uses OpenAI-compatible API)
        self.client = wrap_openai_client(client)
        self.model = config.groq_model

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
                logger.info("Loaded Groq API key from GCP Secret Manager: %s", secret_name)
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
        """Generate a response using Groq."""
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

        response = await self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
