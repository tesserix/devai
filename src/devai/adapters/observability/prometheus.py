"""Prometheus observability provider (HTTP API, no SDK needed)."""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)

logger = logging.getLogger(__name__)


class PrometheusAdapter(ObservabilityAdapter):
    """Reads from a Prometheus HTTP API. Config: {url, token?}."""

    provider_name = "prometheus"

    def __init__(self, config: dict[str, Any]) -> None:
        self._url = (config.get("url") or "").rstrip("/")
        self._token = config.get("token") or ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{self._url}{path}", params=params, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        if not self._url:
            return []
        try:
            end = time.time()
            step = max(15, window_seconds // 200)
            data = await self._get(
                "/api/v1/query_range",
                {"query": query, "start": end - window_seconds, "end": end, "step": step},
            )
        except Exception:  # noqa: BLE001
            logger.warning("prometheus query_metrics failed", exc_info=True)
            return []
        out: list[MetricSeries] = []
        for res in data.get("data", {}).get("result", []):
            labels = res.get("metric", {})
            points = [(float(ts), float(v)) for ts, v in res.get("values", [])]
            out.append(
                MetricSeries(
                    name=labels.get("__name__", query),
                    provider="prometheus",
                    points=points,
                    labels=labels,
                )
            )
        return out

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        # Core Prometheus has no log store; logs come from Loki/Elastic. Return
        # empty rather than pretend. (A Loki provider can be added later.)
        return []

    async def get_alerts(self) -> list[Alert]:
        if not self._url:
            return []
        try:
            data = await self._get("/api/v1/alerts", {})
        except Exception:  # noqa: BLE001
            logger.warning("prometheus get_alerts failed", exc_info=True)
            return []
        out: list[Alert] = []
        for a in data.get("data", {}).get("alerts", []):
            labels = a.get("labels", {})
            out.append(
                Alert(
                    title=labels.get("alertname", "alert"),
                    provider="prometheus",
                    severity=labels.get("severity", ""),
                    state=a.get("state", ""),
                    service=labels.get("service", labels.get("job", "")),
                    description=a.get("annotations", {}).get("description", ""),
                )
            )
        return out

    async def health_check(self) -> ProviderHealth:
        if not self._url:
            return ProviderHealth(provider="prometheus", ok=False, detail="no url configured")
        try:
            await self._get("/api/v1/query", {"query": "vector(1)"})
            return ProviderHealth(provider="prometheus", ok=True, detail=self._url)
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="prometheus", ok=False, detail=str(e)[:200])


__all__ = ["PrometheusAdapter"]
