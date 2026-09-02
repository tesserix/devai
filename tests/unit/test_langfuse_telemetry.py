from __future__ import annotations

import json
from typing import Any

import httpx

from devai.adapters.telemetry.base import EvaluationMetric
from devai.adapters.telemetry.langfuse import LangfuseTelemetryAdapter
from devai.adapters.telemetry.noop import NoopTelemetryAdapter


async def test_langfuse_records_trace_and_dimension_scores_without_payloads() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [{"id": "ok"}], "errors": []})

    client = httpx.AsyncClient(
        base_url="https://langfuse.example",
        transport=httpx.MockTransport(handler),
        auth=("public-test-key", "secret-test-key"),
    )
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="public-test-key",
        secret_key="secret-test-key",
        otel=NoopTelemetryAdapter(),
        client=client,
    )

    await adapter.record_evaluation(
        EvaluationMetric(
            run_id="eval-1",
            agent="weather-agent",
            suite="weather-golden@1",
            pass_rate=0.5,
            case_count=2,
            cost_usd=0.012,
            total_tokens=120,
            p95_latency_ms=420,
            dimensions={"exact_output": 0.75, "tool_trajectory": 1.0},
            failing_case_ids=["rain"],
        )
    )

    batch = requests[0]["batch"]
    assert batch[0]["type"] == "trace-create"
    assert batch[0]["body"]["id"] == "eval-1"
    assert batch[0]["body"]["metadata"]["failing_case_ids"] == ["rain"]
    scores = {event["body"]["name"]: event["body"]["value"] for event in batch[1:]}
    assert scores == {"eval.pass_rate": 0.5, "exact_output": 0.75, "tool_trajectory": 1.0}
    serialized = json.dumps(batch)
    assert "final_text" not in serialized
    assert "input" not in serialized

    await adapter.close()


async def test_langfuse_failure_never_raises_into_the_evaluation_path() -> None:
    client = httpx.AsyncClient(
        base_url="https://langfuse.example",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
        auth=("public-test-key", "secret-test-key"),
    )
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="public-test-key",
        secret_key="secret-test-key",
        otel=NoopTelemetryAdapter(),
        client=client,
    )

    await adapter.record_evaluation(EvaluationMetric(run_id="eval-1", agent="agent", suite="suite@1"))

    await adapter.close()


async def test_langfuse_ingestion_ids_are_deterministic_for_replays() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [{"id": "ok"}], "errors": []})

    client = httpx.AsyncClient(
        base_url="https://langfuse.example",
        transport=httpx.MockTransport(handler),
        auth=("public-test-key", "secret-test-key"),
    )
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="public-test-key",
        secret_key="secret-test-key",
        otel=NoopTelemetryAdapter(),
        client=client,
    )
    metric = EvaluationMetric(run_id="eval-1", agent="agent", suite="suite@1", pass_rate=1.0)

    await adapter.record_evaluation(metric)
    await adapter.record_evaluation(metric)

    first_ids = [event["id"] for event in requests[0]["batch"]]
    second_ids = [event["id"] for event in requests[1]["batch"]]
    # Same evaluation → same event ids, so Langfuse dedupes instead of stacking duplicates.
    assert first_ids == second_ids
    assert len(set(first_ids)) == len(first_ids)

    await adapter.close()


def test_langfuse_routes_metrics_to_the_collector_not_langfuse() -> None:
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="public-test-key",
        secret_key="secret-test-key",
        metrics_endpoint="http://otel-gateway.observability.svc.cluster.local:4318",
        client=httpx.AsyncClient(base_url="https://langfuse.example"),
    )

    otel = adapter._otel
    assert otel._endpoint == "https://langfuse.example/api/public/otel"
    assert otel._metrics_endpoint == "http://otel-gateway.observability.svc.cluster.local:4318"


def test_langfuse_without_a_collector_disables_metric_export() -> None:
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="public-test-key",
        secret_key="secret-test-key",
        client=httpx.AsyncClient(base_url="https://langfuse.example"),
    )

    # No collector endpoint → readerless meter provider, not metrics shipped to Langfuse.
    assert adapter._otel._metrics_endpoint == ""
