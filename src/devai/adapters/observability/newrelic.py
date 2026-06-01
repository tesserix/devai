"""New Relic observability provider (NerdGraph GraphQL + NRQL)."""

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

_NERDGRAPH = "https://api.newrelic.com/graphql"


class NewRelicAdapter(ObservabilityAdapter):
    """Config: {api_key (User key), account_id}."""

    provider_name = "newrelic"

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key = config.get("api_key") or ""
        self._account_id = str(config.get("account_id") or "")

    def _ready(self) -> bool:
        return bool(self._api_key and self._account_id)

    async def _nrql(self, nrql: str) -> list[dict[str, Any]]:
        import httpx

        gql = '{ actor { account(id: %s) { nrql(query: "%s") { results } } } }' % (
            self._account_id,
            nrql.replace('"', '\\"'),
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                _NERDGRAPH,
                headers={"API-Key": self._api_key, "Content-Type": "application/json"},
                json={"query": gql},
            )
            r.raise_for_status()
            data = r.json()
        return data.get("data", {}).get("actor", {}).get("account", {}).get("nrql", {}).get("results", []) or []

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        # `query` is an NRQL string (e.g. "SELECT average(duration) FROM Transaction TIMESERIES").
        if not self._ready():
            return []
        try:
            results = await self._nrql(query)
        except Exception:  # noqa: BLE001
            logger.warning("newrelic query_metrics failed", exc_info=True)
            return []
        points: list[tuple[float, float]] = []
        for row in results:
            ts = float(row.get("beginTimeSeconds", row.get("timestamp", 0)) or 0)
            val = next(
                (float(v) for k, v in row.items() if isinstance(v, (int, float)) and k != "beginTimeSeconds"), None
            )
            if val is not None:
                points.append((ts, val))
        return [MetricSeries(name=query[:60], provider="newrelic", points=points)] if points else []

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        if not self._ready():
            return []
        nrql = f"SELECT timestamp, message, level, service.name FROM Log WHERE message LIKE '%{query}%' LIMIT {min(limit, 1000)}"
        try:
            results = await self._nrql(nrql)
        except Exception:  # noqa: BLE001
            logger.warning("newrelic query_logs failed", exc_info=True)
            return []
        return [
            LogEntry(
                timestamp=float(r.get("timestamp", 0) or 0) / 1000.0,
                message=str(r.get("message", "")),
                provider="newrelic",
                level=str(r.get("level", "")),
                service=str(r.get("service.name", "")),
            )
            for r in results
        ]

    async def get_alerts(self) -> list[Alert]:
        if not self._ready():
            return []
        gql = (
            "{ actor { account(id: %s) { aiIssues { issues(filter: {states: ACTIVATED}) "
            "{ issues { title priority state } } } } } }" % self._account_id
        )
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    _NERDGRAPH,
                    headers={"API-Key": self._api_key, "Content-Type": "application/json"},
                    json={"query": gql},
                )
                r.raise_for_status()
                issues = (
                    r.json()
                    .get("data", {})
                    .get("actor", {})
                    .get("account", {})
                    .get("aiIssues", {})
                    .get("issues", {})
                    .get("issues", [])
                )
        except Exception:  # noqa: BLE001
            logger.warning("newrelic get_alerts failed", exc_info=True)
            return []
        return [
            Alert(
                title=(i.get("title") or ["issue"])[0]
                if isinstance(i.get("title"), list)
                else str(i.get("title", "issue")),
                provider="newrelic",
                severity=str(i.get("priority", "")).lower(),
                state="firing",
            )
            for i in issues
        ]

    async def health_check(self) -> ProviderHealth:
        if not self._ready():
            return ProviderHealth(provider="newrelic", ok=False, detail="api_key + account_id required")
        try:
            await self._nrql("SELECT count(*) FROM Transaction SINCE 1 minute ago")
            return ProviderHealth(provider="newrelic", ok=True, detail=f"account {self._account_id}")
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="newrelic", ok=False, detail=str(e)[:200])


__all__ = ["NewRelicAdapter"]
