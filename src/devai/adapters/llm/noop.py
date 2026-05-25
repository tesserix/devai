"""Noop LLM backend.

Used when:
  - `DEVAI_LLM_PROVIDER=noop` (explicit opt-out)
  - The configured provider's SDK isn't installed
  - The configured provider's required settings are missing
  - Unit tests run without making real API calls

Returns a canned response — same shape as any real adapter, no network
calls, no API costs. Tests can optionally configure a sequence of
responses to return in order via `set_responses([...])`.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from devai.adapters.llm.base import (
    LLMAdapter,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)


class NoopLLMAdapter(LLMAdapter):
    """No network, no cost, predictable output."""

    provider_name = "noop"
    default_model = "noop-1"

    def __init__(self, canned_text: str = "[noop response]") -> None:
        self._canned_text = canned_text
        # Tests can preload a queue of responses; otherwise every
        # generate() call returns the same canned text.
        self._queue: list[LLMResponse] = []

    def set_responses(self, responses: list[LLMResponse]) -> None:
        """Test helper: queue up specific responses to return in order."""
        self._queue = list(responses)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if self._queue:
            resp = self._queue.pop(0)
            # Fill provenance fields if the test didn't set them
            if not resp.provider:
                resp.provider = self.provider_name
            if not resp.model:
                resp.model = request.model or self.default_model
            return resp

        # Fabricate a minimal but well-formed response
        return LLMResponse(
            text=self._canned_text,
            usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            finish_reason="stop",
            model=request.model or self.default_model,
            provider=self.provider_name,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        resp = await self.generate(request)
        # Simulate streaming by yielding the response in two halves
        midpoint = len(resp.text) // 2 if resp.text else 0
        yield LLMResponse(
            text=resp.text[:midpoint],
            model=resp.model,
            provider=resp.provider,
            finish_reason="",
        )
        await asyncio.sleep(0)
        yield resp

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        # Deterministic per-text fake embeddings of dim=4. Useful for
        # tests that exercise the embedding path without needing a real
        # vector model.
        return [[float(hash((t, i)) % 100) / 100.0 for i in range(4)] for t in texts]

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "noop is always up"}


__all__ = ["NoopLLMAdapter"]
