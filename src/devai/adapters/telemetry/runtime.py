"""Process-global telemetry sink.

OpenTelemetry's own providers are process-global (set_tracer_provider /
set_meter_provider), so DevAI mirrors that: the app builds ONE telemetry
adapter at startup and registers it here. Code that can't take constructor
injection (the LLM factory's instrumentation wrapper, tool handlers, …) reads
the sink via `get_global_telemetry()` and always gets a usable adapter — the
Noop until an app registers a real one.

This is NOT a substitute for StageDeps-style injection where that exists
(PipelineService takes `telemetry=` directly); it's the escape hatch for
call sites created outside the app wiring.
"""

from __future__ import annotations

from devai.adapters.telemetry.base import TelemetryAdapter
from devai.adapters.telemetry.noop import NoopTelemetryAdapter

_global: TelemetryAdapter = NoopTelemetryAdapter()


def set_global_telemetry(adapter: TelemetryAdapter | None) -> None:
    """Register the process-wide telemetry sink. None resets to Noop."""
    global _global
    _global = adapter if adapter is not None else NoopTelemetryAdapter()


def get_global_telemetry() -> TelemetryAdapter:
    """The process-wide sink. Never None — Noop until an app registers one."""
    return _global


__all__ = ["get_global_telemetry", "set_global_telemetry"]
