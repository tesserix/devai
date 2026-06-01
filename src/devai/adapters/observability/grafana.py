"""Grafana observability provider (HTTP API; service-account token).

Metrics are proxied through a configured datasource (Prometheus-style
/api/datasources/proxy); alerts come from Grafana unified alerting.
"""

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


class GrafanaAdapter(ObservabilityAdapter):
    """Config: {url, token, datasource_uid?}."""

    provider_name = "grafana"

    def __init__(self, config: dict[str, Any]) -> None:
        self._url = (config.get("url") or "").rstrip("/")
        self._token = config.get("token") or ""
        self._ds = config.get("datasource_uid") or ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        if not self._url or not self._ds:
            return []
        try:
            import httpx

            now_ms = int(time.time() * 1000)
            body = {
                "queries": [
                    {
                        "refId": "A",
                        "expr": query,
                        "datasource": {"uid": self._ds},
                        "intervalMs": 60000,
                        "maxDataPoints": 200,
                    }
                ],
                "from": str(now_ms - window_seconds * 1000),
                "to": str(now_ms),
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(f"{self._url}/api/ds/query", json=body, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("grafana query_metrics failed", exc_info=True)
            return []
        out: list[MetricSeries] = []
        frames = data.get("results", {}).get("A", {}).get("frames", [])
        for fr in frames:
            values = fr.get("data", {}).get("values", [])
            if len(values) >= 2:
                points = [
                    (float(t) / 1000.0, float(v)) for t, v in zip(values[0], values[1], strict=False) if v is not None
                ]
                out.append(MetricSeries(name=query[:60], provider="grafana", points=points))
        return out

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        # Grafana proxies logs from Loki/Elastic datasources; route those via
        # their own connectors. Grafana itself isn't a log store.
        return []

    async def get_alerts(self) -> list[Alert]:
        if not self._url:
            return []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{self._url}/api/prometheus/grafana/api/v1/alerts", headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("grafana get_alerts failed", exc_info=True)
            return []
        out: list[Alert] = []
        for a in data.get("data", {}).get("alerts", []):
            labels = a.get("labels", {})
            if a.get("state") in ("firing", "alerting", "pending"):
                out.append(
                    Alert(
                        title=labels.get("alertname", "alert"),
                        provider="grafana",
                        severity=labels.get("severity", ""),
                        state="firing",
                        service=labels.get("service", ""),
                        description=a.get("annotations", {}).get("description", ""),
                    )
                )
        return out

    async def health_check(self) -> ProviderHealth:
        if not self._url:
            return ProviderHealth(provider="grafana", ok=False, detail="no url configured")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self._url}/api/health", headers=self._headers())
                ok = r.status_code == 200
            return ProviderHealth(provider="grafana", ok=ok, detail=self._url if ok else f"http {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="grafana", ok=False, detail=str(e)[:200])


__all__ = ["GrafanaAdapter"]
