"""Elasticsearch observability provider (HTTP _search; logs + simple metrics)."""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.observability.base import (
    Alert,
    LogEntry,
    MetricSeries,
    ObservabilityAdapter,
    ProviderHealth,
)

logger = logging.getLogger(__name__)


class ElasticsearchAdapter(ObservabilityAdapter):
    """Config: {url, api_key? | username?+password?, index?='logs-*'}."""

    provider_name = "elasticsearch"

    def __init__(self, config: dict[str, Any]) -> None:
        self._url = (config.get("url") or "").rstrip("/")
        self._api_key = config.get("api_key") or ""
        self._user = config.get("username") or ""
        self._password = config.get("password") or ""
        self._index = config.get("index") or "logs-*"

    def _auth(self) -> dict[str, Any]:
        if self._api_key:
            return {"headers": {"Authorization": f"ApiKey {self._api_key}"}}
        if self._user:
            return {"auth": (self._user, self._password)}
        return {}

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        if not self._url:
            return []
        try:
            import httpx

            body = {
                "size": min(limit, 1000),
                "sort": [{"@timestamp": "desc"}],
                "query": {
                    "bool": {
                        "must": [{"query_string": {"query": query}}],
                        "filter": [{"range": {"@timestamp": {"gte": f"now-{window_seconds}s"}}}],
                    }
                },
            }
            async with httpx.AsyncClient(timeout=20.0, **self._auth()) as client:
                r = await client.post(f"{self._url}/{self._index}/_search", json=body)
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("elasticsearch query_logs failed", exc_info=True)
            return []
        out: list[LogEntry] = []
        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            out.append(
                LogEntry(
                    timestamp=0.0,
                    message=str(src.get("message", src.get("msg", ""))),
                    provider="elasticsearch",
                    level=str(src.get("log.level", src.get("level", ""))),
                    service=str(src.get("service.name", src.get("service", ""))),
                    attributes=src,
                )
            )
        return out

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        # `query` = a numeric field to average over a date_histogram on the index.
        if not self._url:
            return []
        try:
            import httpx

            body = {
                "size": 0,
                "query": {"range": {"@timestamp": {"gte": f"now-{window_seconds}s"}}},
                "aggs": {
                    "ts": {
                        "date_histogram": {"field": "@timestamp", "fixed_interval": "1m"},
                        "aggs": {"val": {"avg": {"field": query}}},
                    }
                },
            }
            async with httpx.AsyncClient(timeout=20.0, **self._auth()) as client:
                r = await client.post(f"{self._url}/{self._index}/_search", json=body)
                r.raise_for_status()
                data = r.json()
        except Exception:  # noqa: BLE001
            logger.warning("elasticsearch query_metrics failed", exc_info=True)
            return []
        buckets = data.get("aggregations", {}).get("ts", {}).get("buckets", [])
        points = [
            (float(b.get("key", 0)) / 1000.0, float(b.get("val", {}).get("value") or 0.0))
            for b in buckets
            if b.get("val", {}).get("value") is not None
        ]
        return [MetricSeries(name=query, provider="elasticsearch", points=points)] if points else []

    async def get_alerts(self) -> list[Alert]:
        # Elastic alerting (Watcher/Kibana rules) needs the Kibana API; v1 keeps
        # this empty and relies on metrics/logs for signal.
        return []

    async def health_check(self) -> ProviderHealth:
        if not self._url:
            return ProviderHealth(provider="elasticsearch", ok=False, detail="no url configured")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0, **self._auth()) as client:
                r = await client.get(f"{self._url}/_cluster/health")
                ok = r.status_code == 200
            return ProviderHealth(provider="elasticsearch", ok=ok, detail=self._url if ok else f"http {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="elasticsearch", ok=False, detail=str(e)[:200])


__all__ = ["ElasticsearchAdapter"]
