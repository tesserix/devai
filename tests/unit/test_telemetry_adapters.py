"""Telemetry adapter tests — contract + factory + analytics aggregation.

Layers:

1. **Contract tests** — the adapters constructible in CI (Noop always; OTel
   when the OTLP exporter is installed) satisfy the same ABC: instrument_asgi,
   record_stage/record_llm/incr/observe are total (never raise), and
   health_check returns the canonical dict.

2. **Factory tests** — every value of DEVAI_TELEMETRY_PROVIDER resolves to a
   usable adapter; failure modes (metrics disabled, no endpoint, unknown
   provider, missing SDK) all degrade gracefully to Noop — the factory never
   raises.

3. **Analytics aggregation** — the pure run/stage rollups produce correct KPIs
   from a synthetic task list.
"""

from __future__ import annotations

import pytest

from devai.adapters.telemetry import (
    KNOWN_PROVIDERS,
    LLMMetric,
    NoopTelemetryAdapter,
    StageMetric,
    TelemetryAdapter,
    create_telemetry_adapter,
)
from devai.analytics.service import runs_timeseries, stage_stats, summarize_runs


class _Settings:
    """Minimal settings stub."""

    metrics_enabled = True
    telemetry_provider = "noop"
    otel_endpoint = ""
    otel_service_name = "devai-test"
    otel_service_namespace = "devai"
    otel_export_interval_ms = 15000


# ──────────────────────────────────────────────────────────────────────
# Contract
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_satisfies_contract() -> None:
    a = NoopTelemetryAdapter()
    assert isinstance(a, TelemetryAdapter)
    # All record_* are total — must not raise and must return None.
    assert a.record_stage(StageMetric(blueprint="bp", stage="s", status="completed", duration_ms=12.0)) is None
    assert a.record_llm(LLMMetric(agent="dev", provider="anthropic", model="claude", tokens_input=5)) is None
    assert a.incr("x") is None
    assert a.observe("y", 1.0) is None
    a.instrument_asgi(object())  # no-op, no app needed
    health = await a.health_check()
    assert health["provider"] == "noop"
    assert health["ok"] is True
    assert health["exporting"] is False
    await a.close()


# ──────────────────────────────────────────────────────────────────────
# Factory degradation
# ──────────────────────────────────────────────────────────────────────


def test_factory_default_is_noop() -> None:
    a = create_telemetry_adapter(_Settings())
    assert a.provider_name == "noop"


def test_factory_metrics_disabled_forces_noop() -> None:
    s = _Settings()
    s.metrics_enabled = False
    s.telemetry_provider = "otel"
    s.otel_endpoint = "http://collector:4318"
    a = create_telemetry_adapter(s)
    assert a.provider_name == "noop"


def test_factory_otel_without_endpoint_degrades() -> None:
    s = _Settings()
    s.telemetry_provider = "otel"
    s.otel_endpoint = ""  # missing → AdapterNotConfigured → Noop
    a = create_telemetry_adapter(s)
    assert a.provider_name == "noop"


def test_factory_unknown_provider_degrades() -> None:
    s = _Settings()
    s.telemetry_provider = "datadog"  # not registered yet
    a = create_telemetry_adapter(s)
    assert a.provider_name == "noop"


def test_known_providers() -> None:
    assert "noop" in KNOWN_PROVIDERS
    assert "otel" in KNOWN_PROVIDERS


# ──────────────────────────────────────────────────────────────────────
# Global sink + instrumented LLM delegate
# ──────────────────────────────────────────────────────────────────────


class _RecordingTelemetry(NoopTelemetryAdapter):
    """Noop that remembers every record_llm call."""

    def __init__(self) -> None:
        self.llm_calls: list[LLMMetric] = []

    def record_llm(self, metric: LLMMetric) -> None:
        self.llm_calls.append(metric)


def test_global_sink_defaults_to_noop_and_resets() -> None:
    from devai.adapters.telemetry.runtime import get_global_telemetry, set_global_telemetry

    set_global_telemetry(None)
    assert get_global_telemetry().provider_name == "noop"
    sink = _RecordingTelemetry()
    set_global_telemetry(sink)
    assert get_global_telemetry() is sink
    set_global_telemetry(None)


@pytest.mark.asyncio
async def test_instrumented_llm_adapter_records_usage() -> None:
    from devai.adapters.llm.base import make_request
    from devai.adapters.llm.instrumented import InstrumentedLLMAdapter
    from devai.adapters.llm.noop import NoopLLMAdapter
    from devai.adapters.telemetry.runtime import set_global_telemetry

    sink = _RecordingTelemetry()
    set_global_telemetry(sink)
    try:
        wrapped = InstrumentedLLMAdapter(NoopLLMAdapter())
        req = make_request(user="hello")
        req.extra["agent"] = "senior-developer"
        resp = await wrapped.generate(req)
        assert resp is not None
        assert len(sink.llm_calls) == 1
        m = sink.llm_calls[0]
        assert m.agent == "senior-developer"
        assert m.provider == wrapped.provider_name
        assert m.status == "ok"
        assert m.duration_ms >= 0
    finally:
        set_global_telemetry(None)


def test_factory_wraps_backends_with_instrumentation() -> None:
    from devai.adapters.llm import create_llm_adapter
    from devai.adapters.llm.instrumented import InstrumentedLLMAdapter

    class _S:
        llm_provider = "noop"

    adapter = create_llm_adapter(_S())
    assert isinstance(adapter, InstrumentedLLMAdapter)
    assert adapter.provider_name == "noop"


# ──────────────────────────────────────────────────────────────────────
# Analytics aggregation
# ──────────────────────────────────────────────────────────────────────


def _tasks() -> list[dict]:
    return [
        {
            "id": "1",
            "blueprint": "alm-pipeline",
            "state": "completed",
            "created_at": 1_700_000_000.0,
            "started_at": 1_700_000_000.0,
            "finished_at": 1_700_000_060.0,  # 60s
            "stage_events": [
                {"stage": "implement_code", "phase": "completed", "duration_ms": 40000},
                {"stage": "review_code", "phase": "completed", "duration_ms": 20000},
            ],
        },
        {
            "id": "2",
            "blueprint": "alm-pipeline",
            "state": "failed",
            "created_at": 1_700_086_400.0,
            "started_at": 1_700_086_400.0,
            "finished_at": 1_700_086_430.0,  # 30s
            "stage_events": [
                {"stage": "implement_code", "phase": "failed", "duration_ms": 30000},
            ],
        },
        {
            "id": "3",
            "blueprint": "sre-health-check",
            "state": "running",
            "created_at": 1_700_172_800.0,
            "stage_events": [],
        },
    ]


def test_summarize_runs() -> None:
    out = summarize_runs(_tasks(), window_days=30)
    assert out["runs"]["total"] == 3
    assert out["runs"]["completed"] == 1
    assert out["runs"]["failed"] == 1
    assert out["runs"]["active"] == 1
    # 1 success / 2 terminal = 0.5
    assert out["runs"]["success_rate"] == 0.5
    # avg of 60000ms and 30000ms = 45000
    assert out["runs"]["avg_duration_ms"] == 45000.0
    bps = {r["blueprint"]: r for r in out["by_blueprint"]}
    assert bps["alm-pipeline"]["total"] == 2


def test_runs_timeseries() -> None:
    ts = runs_timeseries(_tasks(), days=30)
    assert len(ts) == 3  # three distinct days
    assert all({"date", "total", "completed", "failed"} <= set(row) for row in ts)


def test_stage_stats() -> None:
    rows = stage_stats(_tasks())
    by_stage = {r["stage"]: r for r in rows}
    assert by_stage["implement_code"]["runs"] == 2
    assert by_stage["implement_code"]["failures"] == 1
    # avg of 40000 and 30000 = 35000
    assert by_stage["implement_code"]["avg_duration_ms"] == 35000.0
