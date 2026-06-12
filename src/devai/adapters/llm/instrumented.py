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
            model = (response.model if response else "") or request.model or self.default_model
            tok_in = getattr(usage, "prompt_tokens", 0) or 0
            tok_out = getattr(usage, "completion_tokens", 0) or 0
            cost = 0.0
            try:
                from devai.analytics.pricing import estimate_cost

                cost = estimate_cost(self.provider_name, model, tok_in, tok_out)
            except Exception:  # noqa: BLE001
                pass
            get_global_telemetry().record_llm(
                LLMMetric(
                    agent=str(request.extra.get("agent", "") or ""),
                    provider=self.provider_name,
                    model=model,
                    tokens_input=tok_in,
                    tokens_output=tok_out,
                    cost_usd=cost,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
            # Queryable usage ledger (Redis) — feeds the analytics cost/tokens/
            # latency views per model AND per user, for every run (blueprint
            # runs don't write agent_executions). Fire-and-forget; never blocks.
            self._ledger_write(request, model, tok_in, tok_out, cost, duration_ms, status)
        except Exception:  # noqa: BLE001 — telemetry must never break the call
            pass

    def _ledger_write(
        self,
        request: LLMRequest,
        model: str,
        tok_in: int,
        tok_out: int,
        cost: float,
        duration_ms: float,
        status: str,
    ) -> None:
        try:
            from devai.analytics.usage_ledger import get_global_ledger

            ledger = get_global_ledger()
            if ledger is None:
                return
            import asyncio
            import datetime

            extra = request.extra or {}
            day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
            coro = ledger.record(
                day=day,
                provider=self.provider_name,
                model=model,
                tokens_in=tok_in,
                tokens_out=tok_out,
                cost_usd=cost,
                duration_ms=duration_ms,
                triggered_by=str(extra.get("triggered_by", "") or ""),
                agent=str(extra.get("agent", "") or ""),
                run_id=str(extra.get("run_id", "") or ""),
                status=status,
            )
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            pass  # no running loop (sync test context) — skip the ledger write
        except Exception:  # noqa: BLE001
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
