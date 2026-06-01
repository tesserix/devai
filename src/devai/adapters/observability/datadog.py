"""Datadog observability provider (HTTP API; api_key + app_key)."""

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


class DatadogAdapter(ObservabilityAdapter):
    """Config: {api_key, app_key, site?='datadoghq.com'}."""

    provider_name = "datadog"

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key = config.get("api_key") or ""
        self._app_key = config.get("app_key") or ""
        self._site = config.get("site") or "datadoghq.com"

    def _headers(self) -> dict[str, str]:
        return {"DD-API-KEY": self._api_key, "DD-APPLICATION-KEY": self._app_key}

    def _ready(self) -> bool:
        return bool(self._api_key and self._app_key)

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        if not self._ready():
            return []
        try:
            import httpx

            end = int(time.time())
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    f"https://api.{self._site}/api/v1/query",
                    params={"from": end - window_seconds, "to": end, "query": query},
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("datadog query_metrics failed", exc_info=True)
            return []
        out: list[MetricSeries] = []
        for s in data.get("series", []):
            points = [(float(p[0]) / 1000.0, float(p[1])) for p in s.get("pointlist", []) if p[1] is not None]
            out.append(
                MetricSeries(
                    name=s.get("metric", query), provider="datadog", points=points, unit=(s.get("unit") or [{}])[0].get("name", "") if s.get("unit") else ""
                )
            )
        return out

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        if not self._ready():
            return []
        try:
            import httpx

            now = int(time.time() * 1000)
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"https://api.{self._site}/api/v2/logs/events/search",
                    headers=self._headers(),
                    json={
                        "filter": {"query": query, "from": str(now - window_seconds * 1000), "to": str(now)},
                        "page": {"limit": min(limit, 1000)},
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("datadog query_logs failed", exc_info=True)
            return []
        out: list[LogEntry] = []
        for ev in data.get("data", []):
            attr = ev.get("attributes", {})
            out.append(
                LogEntry(
                    timestamp=0.0,
                    message=attr.get("message", ""),
                    provider="datadog",
                    level=attr.get("status", ""),
                    service=attr.get("service", ""),
                    attributes=attr.get("attributes", {}),
                )
            )
        return out

    async def get_alerts(self) -> list[Alert]:
        if not self._ready():
            return []
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    f"https://api.{self._site}/api/v1/monitor",
                    params={"group_states": "alert,warn"},
                    headers=self._headers(),
                )
                r.raise_for_status()
                monitors = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("datadog get_alerts failed", exc_info=True)
            return []
        out: list[Alert] = []
        for m in monitors:
            state = m.get("overall_state", "")
            if state in ("Alert", "Warn"):
                out.append(
                    Alert(
                        title=m.get("name", "monitor"),
                        provider="datadog",
                        severity="critical" if state == "Alert" else "warning",
                        state="firing",
                        description=m.get("message", "")[:300],
                    )
                )
        return out

    async def health_check(self) -> ProviderHealth:
        if not self._ready():
            return ProviderHealth(provider="datadog", ok=False, detail="api_key + app_key required")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"https://api.{self._site}/api/v1/validate", headers=self._headers())
                ok = r.status_code == 200
            return ProviderHealth(provider="datadog", ok=ok, detail=self._site if ok else f"http {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="datadog", ok=False, detail=str(e)[:200])


__all__ = ["DatadogAdapter"]
