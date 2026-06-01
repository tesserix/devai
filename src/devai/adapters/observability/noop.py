"""Noop observability adapter — the mandatory graceful-degrade fallback.

Returned by the factory for an unknown provider, a missing SDK, or absent
credentials. Every method is a safe empty result so a stage that queries
observability never crashes when nothing is configured.
"""

from __future__ import annotations

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)


class NoopObservabilityAdapter(ObservabilityAdapter):
    provider_name = "noop"

    def __init__(self, reason: str = "not configured") -> None:
        self._reason = reason

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        return []

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        return []

    async def get_alerts(self) -> list[Alert]:
        return []

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider="noop", ok=False, detail=self._reason)


__all__ = ["NoopObservabilityAdapter"]
