"""TelemetryAdapter ABC + canonical metric records.

The telemetry family is how DevAI emits traces and metrics to an external
backend (an OpenTelemetry collector, today) without any business-logic code
importing an OTel SDK. Callers hold an `Optional[TelemetryAdapter]` and call
its record_* methods; whether that goes to a real OTLP exporter or into the
void is a single env var (`DEVAI_TELEMETRY_PROVIDER`).

The surface is deliberately small and domain-shaped — the things DevAI
actually wants to see:

  - request-level instrumentation of the ASGI app (latency + count)
  - one record per pipeline stage (count, duration, status)
  - one record per LLM call (tokens, cost, duration)
  - a generic counter / histogram escape hatch for everything else

Every method is synchronous and MUST NOT raise — telemetry is best-effort and
can never break the request it's measuring. Backends swallow their own errors
and degrade. The Noop backend satisfies the whole ABC with no-ops.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from devai.adapters.base import Adapter


@dataclass(slots=True)
class StageMetric:
    """One pipeline-stage execution, normalized for emission."""

    blueprint: str
    stage: str
    status: str  # completed | failed | skipped | running
    duration_ms: float = 0.0
    repo: str = ""
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class LLMMetric:
    """One LLM call, normalized for emission."""

    agent: str
    provider: str
    model: str
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"  # ok | error
    attrs: dict[str, str] = field(default_factory=dict)


class TelemetryAdapter(Adapter):
    """Minimum surface every telemetry backend implements.

    A backend that can't do tracing or metrics (the Noop) still satisfies the
    contract — its methods are no-ops. Callers NEVER branch on the backend and
    NEVER need a try/except around a record_* call: the methods are total.
    """

    #: Set by subclass — used in logs and health output.
    provider_name: str = "abstract"

    # ──────────────────────────────────────────────────────────────────
    # App instrumentation
    # ──────────────────────────────────────────────────────────────────

    def instrument_asgi(self, app: Any) -> None:
        """Attach request-level tracing/metrics to a FastAPI/Starlette app.

        Default: no-op (the Noop and any metrics-only backend). The OTel
        backend adds a lightweight middleware that records a span + the
        http.server.duration / request.count instruments per request.
        Idempotent — calling twice must not double-instrument.
        """
        return None

    # ──────────────────────────────────────────────────────────────────
    # Domain records — total, best-effort, never raise
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
    def record_stage(self, metric: StageMetric) -> None:
        """Record one pipeline-stage execution (count + duration + status)."""

    @abstractmethod
    def record_llm(self, metric: LLMMetric) -> None:
        """Record one LLM call (tokens + cost + duration)."""

    @abstractmethod
    def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:
        """Generic monotonic counter escape hatch."""

    @abstractmethod
    def observe(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        """Generic histogram / value-distribution escape hatch."""

    # ──────────────────────────────────────────────────────────────────
    # Adapter contract — defaults so subclasses can stay terse.
    # ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": "no health check implemented",
        }


__all__ = [
    "LLMMetric",
    "StageMetric",
    "TelemetryAdapter",
]
