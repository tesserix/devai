"""Groq provider for fast inference (Llama 3.3 70B).

Used for lightweight tasks like requirements analysis and CI log parsing
where speed matters more than deep reasoning.

Falls back to Anthropic Claude when Groq rate limits or errors occur.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from groq import AsyncGroq

from devai.services.tracing import wrap_openai_client

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


def _fetch_secret_via_adapter(config: Settings, secret_name: str) -> str:
    """Resolve a secret value through the secrets adapter (no gcloud subprocess).

    Uses ``adapters/secrets/factory`` so the lookup runs over the GCP Secret
    Manager SDK with Workload Identity (or whatever ``DEVAI_SECRETS_PROVIDER``
    selects), reading the project from settings rather than a hard-coded id.
    Returns ``""`` on any failure so provider construction degrades gracefully.
    """
    if not secret_name:
        return ""
    try:
        import asyncio

        from devai.adapters.secrets.factory import create_secrets_adapter

        adapter = create_secrets_adapter(config)

        async def _resolve() -> str:
            try:
                value = await adapter.get_secret(secret_name)
            finally:
                await adapter.close()
            return value or ""

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            # Constructed inside an already-running loop (rare): run the
            # coroutine on a dedicated loop in a worker thread to avoid reentry.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(_resolve())).result(timeout=15)
        return asyncio.run(_resolve())
    except Exception as e:  # noqa: BLE001 — never let secret resolution crash boot
        logger.debug("Could not fetch secret %s via adapter: %s", secret_name, e)
        return ""


class GroqProvider:
    """Groq Cloud provider for fast LLM inference with Claude fallback."""

    def __init__(self, config: Settings) -> None:
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required

        self._config = config
        api_key = config.groq_api_key
        if not api_key:
            api_key = self._fetch_from_gcp(config.gcp_secret_groq_api_key)

        client = AsyncGroq(
            api_key=api_key,
            base_url=gateway_base_url(config, "groq", getattr(config, "groq_base_url", "")),
            default_headers={"x-devai-provider": "groq"} if gateway_required(config) else None,
        )
        # Wrap for LangSmith tracing (Groq uses OpenAI-compatible API)
        self.client = wrap_openai_client(client)
        self.model = config.groq_model
        self._fallback_enabled = bool(config.anthropic_api_key)

    def _fetch_from_gcp(self, secret_name: str) -> str:
        """Fetch the API key via the secrets adapter (GCP SM SDK + Workload Identity).

        Routes through ``adapters/secrets/factory`` instead of shelling out to the
        ``gcloud`` CLI, which is absent from the distroless image. The project comes
        from settings (``DEVAI_SECRETS_GCP_PROJECT`` / ``DEVAI_GKE_PROJECT``).
        """
        value = _fetch_secret_via_adapter(self._config, secret_name)
        if value:
            logger.info("Loaded Groq API key from secrets adapter: %s", secret_name)
        return value

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using Groq, falling back to Claude on failure."""
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
            if self._fallback_enabled:
                logger.warning("Groq failed (%s), falling back to Claude", e)
                return await self._fallback_to_claude(prompt, system, max_tokens)
            raise

    async def _fallback_to_claude(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
    ) -> str:
        """Fall back to Anthropic Claude when Groq is unavailable."""
        from devai.providers.anthropic_claude import ClaudeProvider

        claude = ClaudeProvider(self._config)
        return await claude.generate(
            system_prompt=system or "You are a helpful assistant.",
            user_message=prompt,
        )
