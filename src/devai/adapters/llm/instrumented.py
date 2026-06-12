"""Telemetry-instrumented LLM adapter delegate.

Wraps any concrete `LLMAdapter` and records one `LLMMetric` (tokens, cost,
latency, provider/model/agent) per `generate()` / `stream()` call into the
process-global telemetry sink (`adapters.telemetry.runtime`). The factory
applies this wrapper to every backend it builds, so EVERY caller — pipeline
stages, chat, SRE agents, the gateway probe — emits LLM telemetry with zero
changes at the call site.

Attribution: callers that know which agent persona is speaking set
`request.extra["agent"]`; everything else is labeled by provider/model only.

Cost: providers don't return spend, so this records token counts; USD cost
stays a Postgres-side concern (agent_executions.llm_cost_usd). When the sink
is the Noop the overhead is two attribute reads — effectively free.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse


class InstrumentedLLMAdapter(LLMAdapter):
    """Pure delegate: forwards everything, records usage on the way out."""

    def __init__(self, inner: LLMAdapter) -> None:
        self._inner = inner
        self.provider_name = inner.provider_name
        self.default_model = inner.default_model

    # ── Instrumented surface ─────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self._inner.generate(request)
        except Exception:
            self._record(request, None, (time.perf_counter() - started) * 1000.0, status="error")
            raise
        self._record(request, response, (time.perf_counter() - started) * 1000.0)
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        started = time.perf_counter()
        last: LLMResponse | None = None
        try:
            async for chunk in self._inner.stream(request):
                last = chunk
                yield chunk
        except Exception:
            self._record(request, last, (time.perf_counter() - started) * 1000.0, status="error")
            raise
        # Usage arrives on the final chunk for both SDKs.
        self._record(request, last, (time.perf_counter() - started) * 1000.0)

    def _record(self, request: LLMRequest, response: LLMResponse | None, duration_ms: float, *, status: str = "ok") -> None:
        try:
            from devai.adapters.telemetry import LLMMetric
            from devai.adapters.telemetry.runtime import get_global_telemetry

            usage = response.usage if response is not None else None
            get_global_telemetry().record_llm(
                LLMMetric(
                    agent=str(request.extra.get("agent", "") or ""),
                    provider=self.provider_name,
                    model=(response.model if response else "") or request.model or self.default_model,
                    tokens_input=getattr(usage, "prompt_tokens", 0) or 0,
                    tokens_output=getattr(usage, "completion_tokens", 0) or 0,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
        except Exception:  # noqa: BLE001 — telemetry must never break the call
            pass

    # ── Pass-throughs ────────────────────────────────────────────────

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        return await self._inner.embed(texts, model=model)

    async def list_models(self) -> list[dict[str, str]]:
        return await self._inner.list_models()

    async def close(self) -> None:
        await self._inner.close()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()


__all__ = ["InstrumentedLLMAdapter"]
