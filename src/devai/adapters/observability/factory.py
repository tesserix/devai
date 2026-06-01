"""Factory + multi-provider fan-out for the observability family.

`create_observability_adapter(provider, config)` builds one provider and
NEVER raises — unknown provider / missing SDK / bad config all degrade to
Noop. `MultiObservabilityAdapter` fans a query across several providers and
merges the results, so an SRE agent issues one query and gets data from
every source it's allowed to use.

Provider config is sourced from the environment (the DevAI Settings
connector writes the secrets to GCP Secret Manager; the SRE deployment's
ExternalSecret maps them to these env vars — same pattern as every other
DevAI integration). `providers_from_env()` returns the set that has creds.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)
from devai.adapters.observability.noop import NoopObservabilityAdapter

logger = logging.getLogger(__name__)

# provider key → (module, class)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "prometheus": ("devai.adapters.observability.prometheus", "PrometheusAdapter"),
    "datadog": ("devai.adapters.observability.datadog", "DatadogAdapter"),
    "newrelic": ("devai.adapters.observability.newrelic", "NewRelicAdapter"),
    "cloudwatch": ("devai.adapters.observability.cloudwatch", "CloudWatchAdapter"),
    "azure_monitor": ("devai.adapters.observability.azure_monitor", "AzureMonitorAdapter"),
    "elasticsearch": ("devai.adapters.observability.elasticsearch", "ElasticsearchAdapter"),
    "grafana": ("devai.adapters.observability.grafana", "GrafanaAdapter"),
}

SUPPORTED_PROVIDERS = tuple(_PROVIDERS)


def create_observability_adapter(provider: str, config: dict[str, Any] | None = None) -> ObservabilityAdapter:
    """Build one provider adapter. Never raises — degrades to Noop."""
    import importlib

    entry = _PROVIDERS.get((provider or "").lower())
    if entry is None:
        logger.info("observability: unknown provider %r — using noop", provider)
        return NoopObservabilityAdapter(reason=f"unknown provider {provider!r}")
    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)(config or {})
    except Exception:  # noqa: BLE001
        logger.warning("observability: failed to build %s — using noop", provider, exc_info=True)
        return NoopObservabilityAdapter(reason=f"{provider} construction failed")


def _config_from_env(provider: str) -> dict[str, Any]:
    """Read a provider's config from env vars. Keys mirror the DevAI Settings
    connector fields; the SRE ExternalSecret supplies the secret values."""
    g = os.environ.get
    if provider == "prometheus":
        # Defaults to the in-cluster Prometheus (same as the legacy SRE tools),
        # so this provider is live out-of-the-box with no secret to configure.
        return {
            "url": g("DEVAI_PROMETHEUS_URL", "http://prometheus-server.monitoring.svc.cluster.local:80"),
            "token": g("DEVAI_PROMETHEUS_TOKEN", ""),
        }
    if provider == "datadog":
        return {
            "api_key": g("DEVAI_DATADOG_API_KEY", ""),
            "app_key": g("DEVAI_DATADOG_APP_KEY", ""),
            "site": g("DEVAI_DATADOG_SITE", "datadoghq.com"),
        }
    if provider == "newrelic":
        return {"api_key": g("DEVAI_NEWRELIC_API_KEY", ""), "account_id": g("DEVAI_NEWRELIC_ACCOUNT_ID", "")}
    if provider == "cloudwatch":
        return {
            "region": g("DEVAI_AWS_REGION", ""),
            "access_key_id": g("AWS_ACCESS_KEY_ID", ""),
            "secret_access_key": g("AWS_SECRET_ACCESS_KEY", ""),
            "log_group": g("DEVAI_CLOUDWATCH_LOG_GROUP", ""),
        }
    if provider == "azure_monitor":
        return {"workspace_id": g("DEVAI_AZURE_WORKSPACE_ID", ""), "resource_id": g("DEVAI_AZURE_RESOURCE_ID", "")}
    if provider == "elasticsearch":
        return {
            "url": g("DEVAI_ELASTICSEARCH_URL", ""),
            "api_key": g("DEVAI_ELASTICSEARCH_API_KEY", ""),
            "username": g("DEVAI_ELASTICSEARCH_USERNAME", ""),
            "password": g("DEVAI_ELASTICSEARCH_PASSWORD", ""),
            "index": g("DEVAI_ELASTICSEARCH_INDEX", "logs-*"),
        }
    if provider == "grafana":
        return {
            "url": g("DEVAI_GRAFANA_URL", ""),
            "token": g("DEVAI_GRAFANA_TOKEN", ""),
            "datasource_uid": g("DEVAI_GRAFANA_DATASOURCE_UID", ""),
        }
    return {}


def _is_configured(provider: str, config: dict[str, Any]) -> bool:
    """Does this provider have enough creds to be worth querying?"""
    if provider == "prometheus":
        return bool(config.get("url"))
    if provider == "datadog":
        return bool(config.get("api_key") and config.get("app_key"))
    if provider == "newrelic":
        return bool(config.get("api_key") and config.get("account_id"))
    if provider == "cloudwatch":
        # Only when AWS is explicitly intended (region / keys / log group set).
        # boto3's default chain (IRSA) still supplies the actual creds.
        return bool(config.get("region") or config.get("access_key_id") or config.get("log_group"))
    if provider == "azure_monitor":
        return bool(config.get("workspace_id") or config.get("resource_id"))
    if provider == "elasticsearch":
        return bool(config.get("url"))
    if provider == "grafana":
        return bool(config.get("url") and config.get("token"))
    return False


def providers_from_env() -> list[str]:
    """The providers that have credentials present in the environment."""
    return [p for p in SUPPORTED_PROVIDERS if _is_configured(p, _config_from_env(p))]


class MultiObservabilityAdapter:
    """Fan-out across several providers and merge results.

    Built from the env-configured providers, optionally filtered to an
    agent's allowed set. A failing provider yields nothing (its own methods
    swallow errors) and never breaks the others.
    """

    def __init__(self, adapters: list[ObservabilityAdapter]) -> None:
        self._adapters = adapters

    @property
    def providers(self) -> list[str]:
        return [a.provider_name for a in self._adapters]

    @classmethod
    def from_env(cls, allowed: list[str] | None = None) -> MultiObservabilityAdapter:
        chosen = providers_from_env()
        if allowed:
            allow = {a.lower() for a in allowed}
            chosen = [p for p in chosen if p in allow]
        return cls([create_observability_adapter(p, _config_from_env(p)) for p in chosen])

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        results = await asyncio.gather(
            *(a.query_metrics(query, window_seconds=window_seconds) for a in self._adapters),
            return_exceptions=True,
        )
        return [s for r in results if isinstance(r, list) for s in r]

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        results = await asyncio.gather(
            *(a.query_logs(query, limit=limit, window_seconds=window_seconds) for a in self._adapters),
            return_exceptions=True,
        )
        return [e for r in results if isinstance(r, list) for e in r]

    async def get_alerts(self) -> list[Alert]:
        results = await asyncio.gather(*(a.get_alerts() for a in self._adapters), return_exceptions=True)
        return [al for r in results if isinstance(r, list) for al in r]

    async def health(self) -> list[ProviderHealth]:
        results = await asyncio.gather(*(a.health_check() for a in self._adapters), return_exceptions=True)
        out: list[ProviderHealth] = []
        for a, r in zip(self._adapters, results, strict=False):
            if isinstance(r, ProviderHealth):
                out.append(r)
            else:
                out.append(ProviderHealth(provider=a.provider_name, ok=False, detail=str(r)[:200]))
        return out


__all__ = [
    "create_observability_adapter",
    "MultiObservabilityAdapter",
    "providers_from_env",
    "SUPPORTED_PROVIDERS",
]
