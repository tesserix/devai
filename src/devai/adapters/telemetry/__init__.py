"""Telemetry adapter family — pluggable traces/metrics backends.

Public surface:

    TelemetryAdapter                  — ABC every backend implements
    StageMetric / LLMMetric           — adapter-agnostic records
    create_telemetry_adapter(settings) — factory; reads DEVAI_TELEMETRY_PROVIDER

Backends:

    noop  — discards everything; the default, and the graceful-degrade fallback
    otel  — OpenTelemetry OTLP/HTTP exporter (traces + metrics) to a collector

Future drop-ins (the factory + registry don't change to add them):

    langsmith (passthrough), datadog, otlp_grpc, prometheus_pull

Backend SDKs are lazy-imported; a pod that runs with telemetry off never loads
the OTel SDK.
"""

from devai.adapters.telemetry.base import (
    EvaluationMetric,
    LLMMetric,
    StageMetric,
    TelemetryAdapter,
)
from devai.adapters.telemetry.factory import (
    KNOWN_PROVIDERS,
    create_telemetry_adapter,
    telemetry_registry,
)
from devai.adapters.telemetry.noop import NoopTelemetryAdapter
from devai.adapters.telemetry.runtime import (
    get_global_telemetry,
    set_global_telemetry,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "EvaluationMetric",
    "LLMMetric",
    "NoopTelemetryAdapter",
    "StageMetric",
    "TelemetryAdapter",
    "create_telemetry_adapter",
    "get_global_telemetry",
    "set_global_telemetry",
    "telemetry_registry",
]
