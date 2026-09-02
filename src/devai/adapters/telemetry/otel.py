"""OpenTelemetry telemetry backend (OTLP/HTTP).

Exports traces and metrics to an OpenTelemetry collector over OTLP/HTTP. The
collector (tesserix-k8s `charts/thirdparty/otel-collector`) re-exports metrics
to the cluster Prometheus and can fan traces out to a tracing backend later.

Everything OTel is lazy-imported inside `__init__` so a pod that runs with
`DEVAI_TELEMETRY_PROVIDER=noop` (or without the exporter installed) never loads
the SDK. The constructor raises the family's typed errors so the factory can
degrade to Noop:

  - exporter SDK missing  → AdapterNotInstalled
  - no endpoint           → AdapterNotConfigured

Instruments created once, reused for the process lifetime. All record_* methods
swallow their own exceptions — measuring a request must never break it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.telemetry.base import LLMMetric, StageMetric, TelemetryAdapter

logger = logging.getLogger(__name__)


def _clean_attrs(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """OTel span/metric attributes must be scalars — coerce and drop empties."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None or value == "":
            continue
        out[key] = value if isinstance(value, (str, bool, int, float)) else str(value)
    return out


def _normalize_signal_endpoint(endpoint: str, signal: str) -> str:
    """Return the OTLP/HTTP URL for a signal.

    The collector exposes `/v1/traces` and `/v1/metrics` on its OTLP/HTTP
    port. Callers configure just the base (`http://otel-collector:4318`); we
    append the signal path. If a full path is already given, respect it.
    """
    base = endpoint.rstrip("/")
    if base.endswith(("/v1/traces", "/v1/metrics")):
        return base
    return f"{base}/v1/{signal}"


class OtelTelemetryAdapter(TelemetryAdapter):
    """OTLP/HTTP exporter for traces + metrics."""

    provider_name = "otel"

    def __init__(
        self,
        *,
        endpoint: str,
        service_name: str = "devai",
        service_namespace: str = "devai",
        export_interval_ms: int = 15000,
        headers: dict[str, str] | None = None,
        metrics_endpoint: str | None = None,
        metrics_headers: dict[str, str] | None = None,
    ) -> None:
        """`metrics_endpoint` routes metrics separately from traces (Langfuse
        accepts only OTLP traces, so its adapter sends metrics to the cluster
        collector instead). None inherits `endpoint`; "" disables metric export
        entirely rather than shipping to an endpoint that drops them."""
        if not endpoint:
            raise AdapterNotConfigured("otel telemetry adapter requires DEVAI_OTEL_ENDPOINT")

        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as e:  # pragma: no cover - exercised via factory degrade
            raise AdapterNotInstalled(
                "otel telemetry adapter requires opentelemetry-exporter-otlp-proto-http (pip install 'devai[otel]')"
            ) from e

        self._endpoint = endpoint
        self._headers = headers or {}
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": service_namespace,
            }
        )

        # Traces — batched span processor over OTLP/HTTP.
        span_exporter = OTLPSpanExporter(
            endpoint=_normalize_signal_endpoint(endpoint, "traces"),
            headers=self._headers or None,
        )
        self._tracer_provider = TracerProvider(resource=resource)
        self._tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        # Globals are set best-effort for third-party instrumentation, but the
        # adapter always uses ITS OWN providers — a second construction (reload,
        # tests) must not silently keep exporting through the first one.
        trace.set_tracer_provider(self._tracer_provider)
        self._tracer = self._tracer_provider.get_tracer("devai")
        self._trace_api = trace

        # Metrics — periodic reader over OTLP/HTTP, possibly to a different
        # endpoint than traces. "" builds a readerless provider: instruments
        # work, nothing is exported.
        resolved_metrics_endpoint = endpoint if metrics_endpoint is None else metrics_endpoint
        readers = []
        if resolved_metrics_endpoint:
            metric_exporter = OTLPMetricExporter(
                endpoint=_normalize_signal_endpoint(resolved_metrics_endpoint, "metrics"),
                headers=(metrics_headers if metrics_endpoint is not None else self._headers) or None,
            )
            readers.append(
                PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=export_interval_ms,
                )
            )
        self._metrics_endpoint = resolved_metrics_endpoint
        self._meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(self._meter_provider)
        meter = self._meter_provider.get_meter("devai")

        # Pre-declared instruments (the domain-shaped ones).
        self._req_count = meter.create_counter(
            "devai.http.server.requests", unit="1", description="HTTP requests served"
        )
        self._req_duration = meter.create_histogram(
            "devai.http.server.duration", unit="ms", description="HTTP request latency"
        )
        self._stage_count = meter.create_counter(
            "devai.pipeline.stage.runs", unit="1", description="Pipeline stage executions"
        )
        self._stage_duration = meter.create_histogram(
            "devai.pipeline.stage.duration", unit="ms", description="Pipeline stage latency"
        )
        self._llm_tokens = meter.create_counter("devai.llm.tokens", unit="1", description="LLM tokens consumed")
        self._llm_cost = meter.create_counter("devai.llm.cost_usd", unit="USD", description="LLM spend in USD")
        self._llm_duration = meter.create_histogram("devai.llm.duration", unit="ms", description="LLM call latency")
        # Per-fleet (per-agent) execution counter — lets Grafana break runs
        # and failures down by agent persona independent of the stage name.
        self._agent_runs = meter.create_counter(
            "devai.agent.runs", unit="1", description="Agent (fleet) stage executions"
        )

        self._meter = meter
        # Generic instrument caches for incr()/observe().
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._instrumented = False
        logger.info("OtelTelemetryAdapter active: endpoint=%s service=%s", endpoint, service_name)

    # ──────────────────────────────────────────────────────────────────
    # App instrumentation
    # ──────────────────────────────────────────────────────────────────

    def instrument_asgi(self, app: Any) -> None:
        if self._instrumented:
            return
        self._instrumented = True
        tracer = self._tracer
        req_count = self._req_count
        req_duration = self._req_duration
        status_code = self._trace_api.StatusCode

        async def _otel_request_middleware(
            request: Any,
            call_next: Callable[[Any], Awaitable[Any]],
        ) -> Any:
            route = request.url.path
            method = request.method
            started = time.perf_counter()
            with tracer.start_as_current_span(f"{method} {route}") as span:
                span.set_attribute("http.method", method)
                span.set_attribute("http.route", route)
                try:
                    response = await call_next(request)
                except Exception as exc:  # noqa: BLE001
                    span.set_status(status_code.ERROR, str(exc))
                    _safe_record(req_count, req_duration, method, route, 500, (time.perf_counter() - started) * 1000)
                    raise
                code = getattr(response, "status_code", 0)
                span.set_attribute("http.status_code", code)
                if code >= 500:
                    span.set_status(status_code.ERROR)
                _safe_record(req_count, req_duration, method, route, code, (time.perf_counter() - started) * 1000)
                return response

        app.middleware("http")(_otel_request_middleware)

    # ──────────────────────────────────────────────────────────────────
    # Domain records
    # ──────────────────────────────────────────────────────────────────

    def span(self, name: str, *, attributes: dict[str, Any] | None = None) -> contextlib.AbstractContextManager[Any]:
        try:
            return self._tracer.start_as_current_span(name, attributes=_clean_attrs(attributes))
        except Exception:  # noqa: BLE001 — tracing must never break the caller
            logger.debug("span(%s) failed to start", name, exc_info=True)
            return contextlib.nullcontext(None)

    def record_stage(self, metric: StageMetric) -> None:
        try:
            attrs = _clean_attrs(
                {
                    "blueprint": metric.blueprint,
                    "stage": metric.stage,
                    "status": metric.status,
                    "agent": metric.agent,
                    "lane": metric.lane,
                    **metric.attrs,
                }
            )
            self._stage_count.add(1, attrs)
            if metric.duration_ms:
                self._stage_duration.record(metric.duration_ms, attrs)
            # Per-agent (fleet) counter — agent-keyed, no stage dimension so the
            # cardinality stays bounded by the number of agent personas.
            if metric.agent:
                self._agent_runs.add(1, _clean_attrs({"agent": metric.agent, "status": metric.status}))
        except Exception:  # noqa: BLE001
            logger.debug("record_stage failed", exc_info=True)

    def record_llm(self, metric: LLMMetric) -> None:
        try:
            attrs = {
                "agent": metric.agent,
                "provider": metric.provider,
                "model": metric.model,
                "status": metric.status,
                **metric.attrs,
            }
            total_tokens = (metric.tokens_input or 0) + (metric.tokens_output or 0)
            if total_tokens:
                self._llm_tokens.add(total_tokens, attrs)
            if metric.cost_usd:
                self._llm_cost.add(metric.cost_usd, attrs)
            if metric.duration_ms:
                self._llm_duration.record(metric.duration_ms, attrs)
        except Exception:  # noqa: BLE001
            logger.debug("record_llm failed", exc_info=True)

    def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:
        try:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name)
                self._counters[name] = counter
            counter.add(value, attrs or {})
        except Exception:  # noqa: BLE001
            logger.debug("incr(%s) failed", name, exc_info=True)

    def observe(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        try:
            hist = self._histograms.get(name)
            if hist is None:
                hist = self._meter.create_histogram(name)
                self._histograms[name] = hist
            hist.record(value, attrs or {})
        except Exception:  # noqa: BLE001
            logger.debug("observe(%s) failed", name, exc_info=True)

    def gauge(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        try:
            gauge = self._gauges.get(name)
            if gauge is None:
                gauge = self._meter.create_gauge(name)
                self._gauges[name] = gauge
            gauge.set(value, attrs or {})
        except Exception:  # noqa: BLE001
            logger.debug("gauge(%s) failed", name, exc_info=True)

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        for provider in (self._tracer_provider, self._meter_provider):
            try:
                provider.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("telemetry provider shutdown failed", exc_info=True)

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": f"exporting OTLP/HTTP to {self._endpoint}",
            "exporting": True,
            "endpoint": self._endpoint,
            "metrics_endpoint": self._metrics_endpoint,
        }


def _safe_record(req_count: Any, req_duration: Any, method: str, route: str, code: int, ms: float) -> None:
    try:
        attrs = {"http.method": method, "http.route": route, "http.status_code": code}
        req_count.add(1, attrs)
        req_duration.record(ms, attrs)
    except Exception:  # noqa: BLE001
        logger.debug("http metric record failed", exc_info=True)


__all__ = ["OtelTelemetryAdapter"]
