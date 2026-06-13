"""Noop telemetry backend.

Used when:
  - `DEVAI_TELEMETRY_PROVIDER=noop` (explicit opt-out)
  - `DEVAI_METRICS_ENABLED=false`
  - the OTel exporter SDK isn't installed (graceful degrade)
  - `DEVAI_OTEL_ENDPOINT` is unset (nothing to export to)
  - unit tests run without a collector

Every method is a no-op. It is the canonical fallback and the default — DevAI
ships fully functional with telemetry off, and turning it on is one env var.
"""

from __future__ import annotations

import contextlib
from typing import Any

from devai.adapters.telemetry.base import LLMMetric, StageMetric, TelemetryAdapter


class NoopTelemetryAdapter(TelemetryAdapter):
    """Discards everything. No tracing, no metrics, no network."""

    provider_name = "noop"

    def instrument_asgi(self, app: Any) -> None:  # noqa: ARG002
        return None

    def span(self, name: str, *, attributes: dict[str, Any] | None = None) -> contextlib.AbstractContextManager[Any]:  # noqa: ARG002
        return contextlib.nullcontext(None)

    def record_stage(self, metric: StageMetric) -> None:  # noqa: ARG002
        return None

    def record_llm(self, metric: LLMMetric) -> None:  # noqa: ARG002
        return None

    def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:  # noqa: ARG002
        return None

    def observe(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:  # noqa: ARG002
        return None

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": "telemetry disabled (noop)",
            "exporting": False,
            "endpoint": "",
        }


__all__ = ["NoopTelemetryAdapter"]
