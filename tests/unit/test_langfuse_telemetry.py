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
        auth=("pk-test", "sk-test"),
    )
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="pk-test",
        secret_key="sk-test",
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
        auth=("pk-test", "sk-test"),
    )
    adapter = LangfuseTelemetryAdapter(
        base_url="https://langfuse.example",
        public_key="pk-test",
        secret_key="sk-test",
        otel=NoopTelemetryAdapter(),
        client=client,
    )

    await adapter.record_evaluation(EvaluationMetric(run_id="eval-1", agent="agent", suite="suite@1"))

    await adapter.close()
