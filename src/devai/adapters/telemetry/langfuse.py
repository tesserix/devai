"""Langfuse telemetry backend using OTLP for traces and ingestion for scores."""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from devai.adapters.base import AdapterNotConfigured
from devai.adapters.telemetry.base import EvaluationMetric, LLMMetric, StageMetric, TelemetryAdapter
from devai.adapters.telemetry.otel import OtelTelemetryAdapter

logger = logging.getLogger(__name__)


class LangfuseTelemetryAdapter(TelemetryAdapter):
    provider_name = "langfuse"

    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        service_name: str = "devai",
        service_namespace: str = "devai",
        deployment_environment: str = "prod",
        export_interval_ms: int = 15000,
        metrics_endpoint: str = "",
        otel: TelemetryAdapter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not public_key or not secret_key:
            raise AdapterNotConfigured(
                "langfuse telemetry requires DEVAI_LANGFUSE_BASE_URL, DEVAI_LANGFUSE_PUBLIC_KEY, and DEVAI_LANGFUSE_SECRET_KEY"
            )
        self._base_url = base_url.rstrip("/")
        authorization = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        # Langfuse's OTLP endpoint accepts traces only. Metrics go to the
        # cluster collector (DEVAI_OTEL_ENDPOINT) without the Langfuse auth
        # header; with no collector configured they are disabled, not shipped
        # to an endpoint that drops them.
        self._otel = otel or OtelTelemetryAdapter(
            endpoint=f"{self._base_url}/api/public/otel",
            service_name=service_name,
            service_namespace=service_namespace,
            deployment_environment=deployment_environment,
            export_interval_ms=export_interval_ms,
            headers={"Authorization": f"Basic {authorization}"},
            metrics_endpoint=metrics_endpoint,
            metrics_headers=None,
        )
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            auth=(public_key, secret_key),
            timeout=5.0,
        )

    def instrument_asgi(self, app: Any) -> None:
        self._otel.instrument_asgi(app)

    def span(self, name: str, *, attributes: dict[str, Any] | None = None):
        return self._otel.span(name, attributes=attributes)

    def record_stage(self, metric: StageMetric) -> None:
        self._otel.record_stage(metric)

    def record_llm(self, metric: LLMMetric) -> None:
        self._otel.record_llm(metric)

    def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:
        self._otel.incr(name, value, attrs)

    def observe(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        self._otel.observe(name, value, attrs)

    def gauge(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        self._otel.gauge(name, value, attrs)

    async def record_evaluation(self, metric: EvaluationMetric) -> None:
        await super().record_evaluation(metric)
        timestamp = datetime.now(UTC).isoformat()
        trace_body = {
            "id": metric.run_id,
            "name": "devai.evaluation",
            "timestamp": timestamp,
            "metadata": {
                "agent": metric.agent,
                "suite": metric.suite,
                "case_count": metric.case_count,
                "cost_usd": metric.cost_usd,
                "total_tokens": metric.total_tokens,
                "p95_latency_ms": metric.p95_latency_ms,
                "failing_case_ids": metric.failing_case_ids,
            },
            "tags": ["devai", "evaluation", metric.agent],
        }
        scores = {"eval.pass_rate": metric.pass_rate, **metric.dimensions}
        # Deterministic ids: Langfuse ingestion dedupes on event id, so
        # re-emitting the same evaluation (retry, replayed run) upserts
        # instead of stacking duplicate scores on the trace.
        batch = [
            {
                "id": _event_id(metric.run_id, "trace"),
                "timestamp": timestamp,
                "type": "trace-create",
                "body": trace_body,
            },
            *[
                {
                    "id": _event_id(metric.run_id, f"score:{name}"),
                    "timestamp": timestamp,
                    "type": "score-create",
                    "body": {
                        "id": _event_id(metric.run_id, f"score:{name}"),
                        "traceId": metric.run_id,
                        "name": name,
                        "value": value,
                        "dataType": "NUMERIC",
                    },
                }
                for name, value in scores.items()
            ],
        ]
        try:
            response = await self._client.post("/api/public/ingestion", json={"batch": batch})
            response.raise_for_status()
        except Exception:  # noqa: BLE001 — evaluation persistence must not depend on telemetry
            logger.warning("Langfuse evaluation export failed for %s", metric.run_id, exc_info=True)

    async def close(self) -> None:
        await self._otel.close()
        await self._client.aclose()

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": f"exporting traces and evaluation scores to {self._base_url}",
            "exporting": True,
            "endpoint": self._base_url,
        }


def _event_id(run_id: str, kind: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"devai-eval:{run_id}:{kind}"))


__all__ = ["LangfuseTelemetryAdapter"]
