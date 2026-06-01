"""Observability adapter family — vendor-neutral metrics/logs/alerts.

Providers: prometheus, datadog, newrelic, cloudwatch, azure_monitor,
elasticsearch, grafana, noop. Configured in DevAI Settings (creds → GCP
Secret Manager → env), consumed by the SRE runtime via a fan-out tool so
an agent can pull from one or many sources with a single query.
"""

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)
from devai.adapters.observability.factory import (
    SUPPORTED_PROVIDERS,
    MultiObservabilityAdapter,
    create_observability_adapter,
    providers_from_env,
)
from devai.adapters.observability.noop import NoopObservabilityAdapter

__all__ = [
    "ObservabilityAdapter",
    "MetricSeries",
    "LogEntry",
    "Alert",
    "ProviderHealth",
    "NoopObservabilityAdapter",
    "create_observability_adapter",
    "MultiObservabilityAdapter",
    "providers_from_env",
    "SUPPORTED_PROVIDERS",
]
