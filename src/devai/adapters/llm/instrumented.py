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

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class InstrumentedLLMAdapter(LLMAdapter):
    """Pure delegate: forwards everything, records usage on the way out.

    Also the single chokepoint for model-id POLICY (see
    ``adapters.llm.model_policy``): every call is coerced onto a model this
    provider can serve — Fable ids fall back to 4.8, and a cross-family pin
    (e.g. a claude id reaching an OpenAI adapter) degrades to the provider's
    own default instead of a 4xx. Because the factory wraps every backend in
    this delegate, the policy holds for EVERY caller with no call-site change.
    """

    # Once-per-(provider, original-model) so a recurring coercion logs once.
    _COERCE_WARNED: set[tuple[str, str]] = set()

    def __init__(self, inner: LLMAdapter) -> None:
        self._inner = inner
        self.provider_name = inner.provider_name
        self.default_model = inner.default_model

    def _coerce_model(self, request: LLMRequest) -> LLMRequest:
        """Keep the call on a model this provider can serve. No-op when the
        request carries no model or already fits the provider."""
        original = request.model or ""
        if not original:
            return request
        from devai.adapters.llm.model_policy import coerce_model

        coerced = coerce_model(self.provider_name, original)
        if coerced == original:
            return request
        sig = (self.provider_name, original)
        if sig not in self._COERCE_WARNED:
            self._COERCE_WARNED.add(sig)
            logger.info(
                "llm model policy: %r is not served by provider %s — using %s",
                original,
                self.provider_name,
                coerced or "the provider default",
            )
        from dataclasses import replace

        return replace(request, model=coerced)

    # ── Instrumented surface ─────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        request = self._coerce_model(request)
        request = self._with_turn_context(request)
        started = time.perf_counter()
        try:
            # Two child trace planes, both nesting under the executor's stage
            # span via contextvars (no-ops when off): an OTel span → Tempo,
            # and a LangSmith run → the LangSmith project.
            with self._otel_span(request), self._trace_ctx(request) as rt:
                response = await self._inner.generate(request)
                if rt is not None:
                    self._trace_end(rt, response)
        except Exception:
            self._record(request, None, (time.perf_counter() - started) * 1000.0, status="error")
            raise
        self._record(request, response, (time.perf_counter() - started) * 1000.0)
        return response

    def _otel_span(self, request: LLMRequest) -> Any:
        """OTel span for one LLM call — nests under the stage span in Tempo."""
        try:
            from devai.adapters.telemetry.runtime import get_global_telemetry

            extra = request.extra or {}
            return get_global_telemetry().span(
                f"llm.{extra.get('agent') or self.provider_name}",
                attributes={
                    "devai.provider": self.provider_name,
                    "devai.model": request.model or self.default_model,
                    "devai.agent": str(extra.get("agent", "") or ""),
                    "devai.run_id": str(extra.get("run_id", "") or ""),
                    "devai.triggered_by": str(extra.get("triggered_by", "") or ""),
                    "devai.tenant_id": str(extra.get("tenant_id", "") or ""),
                    "devai.user_id": str(extra.get("user_id", "") or ""),
                },
            )
        except Exception:  # noqa: BLE001 — tracing must never break the call
            import contextlib as _c

            return _c.nullcontext(None)

    def _trace_ctx(self, request: LLMRequest) -> Any:
        try:
            from devai.services.tracing import is_tracing_enabled

            if not is_tracing_enabled():
                import contextlib

                return contextlib.nullcontext(None)
            from langsmith.run_helpers import trace

            extra = request.extra or {}
            return trace(
                name=f"llm:{extra.get('agent') or self.provider_name}",
                run_type="llm",
                inputs={"model": request.model or self.default_model},
                metadata={
                    "provider": self.provider_name,
                    "run_id": str(extra.get("run_id", "") or ""),
                    "agent": str(extra.get("agent", "") or ""),
                    "triggered_by": str(extra.get("triggered_by", "") or ""),
                    "tenant_id": str(extra.get("tenant_id", "") or ""),
                    "user_id": str(extra.get("user_id", "") or ""),
                },
            )
        except Exception:  # noqa: BLE001 — tracing must never break the call
            import contextlib

            return contextlib.nullcontext(None)

    @staticmethod
    def _trace_end(rt: Any, response: LLMResponse) -> None:
        try:
            usage = response.usage
            rt.end(
                outputs={
                    "tokens_in": getattr(usage, "prompt_tokens", 0) or 0,
                    "tokens_out": getattr(usage, "completion_tokens", 0) or 0,
                }
            )
        except Exception:  # noqa: BLE001
            pass

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        request = self._coerce_model(request)
        request = self._with_turn_context(request)
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

    def _record(
        self, request: LLMRequest, response: LLMResponse | None, duration_ms: float, *, status: str = "ok"
    ) -> None:
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
                tenant_id=str(extra.get("tenant_id", "") or ""),
                user_id=str(extra.get("user_id", "") or ""),
                agent=str(extra.get("agent", "") or ""),
                run_id=str(extra.get("run_id", "") or ""),
                status=status,
                sandbox_id=str(extra.get("sandbox_id", "") or ""),
            )
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
            # Same call, durable sink: one completed agent_executions row in
            # Postgres — what the analytics rollups (top agents by cost, cost
            # by model, cost timeseries, agent table) aggregate. Blueprint
            # runs write nothing else there.
            loop.create_task(
                self._persist_execution_row(
                    model=model,
                    tok_in=tok_in,
                    tok_out=tok_out,
                    cost=cost,
                    duration_ms=duration_ms,
                    status=status,
                    agent=str(extra.get("agent", "") or ""),
                    run_id=str(extra.get("run_id", "") or ""),
                    tenant_id=str(extra.get("tenant_id", "") or ""),
                    user_id=str(extra.get("user_id", "") or ""),
                    triggered_by=str(extra.get("triggered_by", "") or ""),
                )
            )
        except RuntimeError:
            pass  # no running loop (sync test context) — skip the ledger write
        except Exception:  # noqa: BLE001
            pass

    async def _persist_execution_row(
        self,
        *,
        model: str,
        tok_in: int,
        tok_out: int,
        cost: float,
        duration_ms: float,
        status: str,
        agent: str,
        run_id: str,
        tenant_id: str,
        user_id: str,
        triggered_by: str,
    ) -> None:
        try:
            from devai.services.database import get_global_db

            db = await get_global_db()
            if db is None:
                return
            await db.record_llm_call(
                run_id=run_id,
                agent_name=agent or self.provider_name,
                provider=self.provider_name,
                model=model,
                tokens_input=tok_in,
                tokens_output=tok_out,
                cost_usd=cost,
                duration_ms=duration_ms,
                status=status,
                tenant_id=tenant_id,
                user_id=user_id,
                triggered_by=triggered_by,
            )
        except Exception:  # noqa: BLE001 — analytics persistence is best-effort
            pass

    @staticmethod
    def _with_turn_context(request: LLMRequest) -> LLMRequest:
        from dataclasses import replace

        from devai.services.agent_turns import get_turn_context

        ambient = get_turn_context()
        if not ambient:
            return request
        return replace(request, extra={**ambient, **dict(request.extra or {})})

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
