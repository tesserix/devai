"""Azure Monitor / Application Insights provider (azure SDK, lazy import).

Logs (KQL) via LogsQueryClient against an App Insights / Log Analytics
workspace; metrics via MetricsQueryClient against a resource. Auth uses
DefaultAzureCredential (env / workload identity / managed identity).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)

logger = logging.getLogger(__name__)


class AzureMonitorAdapter(ObservabilityAdapter):
    """Config: {workspace_id, resource_id?, subscription_id?}."""

    provider_name = "azure_monitor"

    def __init__(self, config: dict[str, Any]) -> None:
        self._workspace_id = config.get("workspace_id") or ""
        self._resource_id = config.get("resource_id") or ""

    def _credential(self) -> Any:
        from azure.identity import DefaultAzureCredential  # lazy

        return DefaultAzureCredential()

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        if not self._resource_id:
            return []

        def _run() -> list[MetricSeries]:
            from azure.monitor.query import MetricsQueryClient

            client = MetricsQueryClient(self._credential())
            resp = client.query_resource(
                self._resource_id,
                metric_names=[query],
                timespan=timedelta(seconds=window_seconds),
            )
            out: list[MetricSeries] = []
            for metric in resp.metrics:
                for ts_data in metric.timeseries:
                    points = [
                        (d.timestamp.timestamp(), float(d.average if d.average is not None else 0.0))
                        for d in ts_data.data
                    ]
                    out.append(MetricSeries(name=metric.name, provider="azure_monitor", points=points))
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            logger.warning("azure_monitor query_metrics failed", exc_info=True)
            return []

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        if not self._workspace_id:
            return []

        def _run() -> list[LogEntry]:
            from azure.monitor.query import LogsQueryClient

            client = LogsQueryClient(self._credential())
            # `query` may be raw KQL; otherwise wrap a trace search.
            kql = query if " " in query and "|" in query else f'traces | where message contains "{query}" | take {min(limit, 1000)}'
            resp = client.query_workspace(self._workspace_id, kql, timespan=timedelta(seconds=window_seconds))
            out: list[LogEntry] = []
            for table in getattr(resp, "tables", []) or []:
                cols = [c for c in table.columns]
                for row in table.rows:
                    rec = dict(zip(cols, row, strict=False))
                    out.append(
                        LogEntry(
                            timestamp=0.0,
                            message=str(rec.get("message", rec)),
                            provider="azure_monitor",
                            level=str(rec.get("severityLevel", "")),
                        )
                    )
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            logger.warning("azure_monitor query_logs failed", exc_info=True)
            return []

    async def get_alerts(self) -> list[Alert]:
        # Azure alerts live in Azure Monitor Alerts (azure-mgmt-monitor). Kept
        # empty for v1 — wire the mgmt client when alert ingestion is needed.
        return []

    async def health_check(self) -> ProviderHealth:
        if not self._workspace_id and not self._resource_id:
            return ProviderHealth(provider="azure_monitor", ok=False, detail="workspace_id or resource_id required")

        def _run() -> ProviderHealth:
            self._credential()  # constructs + validates the credential chain
            return ProviderHealth(provider="azure_monitor", ok=True, detail=self._workspace_id or self._resource_id)

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="azure_monitor", ok=False, detail=str(e)[:200])


__all__ = ["AzureMonitorAdapter"]
