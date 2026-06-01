"""AWS CloudWatch observability provider (boto3, lazy import).

Metrics query uses the CloudWatch Metrics Insights SQL dialect via
get_metric_data; logs use CloudWatch Logs Insights; alerts are CloudWatch
alarms in ALARM state. Credentials follow the standard boto3 chain
(env / IRSA / profile); ``region`` selects the region.
"""

from __future__ import annotations

import asyncio
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


class CloudWatchAdapter(ObservabilityAdapter):
    """Config: {region, access_key_id?, secret_access_key?, log_group?}."""

    provider_name = "cloudwatch"

    def __init__(self, config: dict[str, Any]) -> None:
        self._region = config.get("region") or "us-east-1"
        self._key = config.get("access_key_id") or ""
        self._secret = config.get("secret_access_key") or ""
        self._log_group = config.get("log_group") or ""

    def _client(self, service: str) -> Any:
        import boto3  # lazy

        kw: dict[str, Any] = {"region_name": self._region}
        if self._key and self._secret:
            kw["aws_access_key_id"] = self._key
            kw["aws_secret_access_key"] = self._secret
        return boto3.client(service, **kw)

    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        # `query` is a Metrics Insights SQL string.
        def _run() -> list[MetricSeries]:
            cw = self._client("cloudwatch")
            end = time.time()
            resp = cw.get_metric_data(
                MetricDataQueries=[{"Id": "q1", "Expression": query, "Period": max(60, window_seconds // 100)}],
                StartTime=end - window_seconds,
                EndTime=end,
            )
            out: list[MetricSeries] = []
            for res in resp.get("MetricDataResults", []):
                pts = list(zip((t.timestamp() for t in res.get("Timestamps", [])), res.get("Values", []), strict=False))
                out.append(
                    MetricSeries(
                        name=res.get("Label", query),
                        provider="cloudwatch",
                        points=[(float(a), float(b)) for a, b in pts],
                    )
                )
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            logger.warning("cloudwatch query_metrics failed", exc_info=True)
            return []

    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        if not self._log_group:
            return []

        def _run() -> list[LogEntry]:
            logs = self._client("logs")
            end = int(time.time())
            start_resp = logs.start_query(
                logGroupName=self._log_group,
                startTime=end - window_seconds,
                endTime=end,
                queryString=f"fields @timestamp, @message | filter @message like /{query}/ | limit {min(limit, 1000)}",
            )
            qid = start_resp["queryId"]
            for _ in range(20):
                time.sleep(0.5)
                res = logs.get_query_results(queryId=qid)
                if res.get("status") in ("Complete", "Failed", "Cancelled"):
                    break
            out: list[LogEntry] = []
            for row in res.get("results", []):
                fields = {c["field"]: c["value"] for c in row}
                out.append(
                    LogEntry(
                        timestamp=0.0,
                        message=fields.get("@message", ""),
                        provider="cloudwatch",
                        service=self._log_group,
                    )
                )
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            logger.warning("cloudwatch query_logs failed", exc_info=True)
            return []

    async def get_alerts(self) -> list[Alert]:
        def _run() -> list[Alert]:
            cw = self._client("cloudwatch")
            resp = cw.describe_alarms(StateValue="ALARM")
            out: list[Alert] = []
            for a in resp.get("MetricAlarms", []):
                out.append(
                    Alert(
                        title=a.get("AlarmName", "alarm"),
                        provider="cloudwatch",
                        severity="high",
                        state="firing",
                        description=a.get("AlarmDescription", "") or a.get("StateReason", ""),
                    )
                )
            return out

        try:
            return await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001
            logger.warning("cloudwatch get_alerts failed", exc_info=True)
            return []

    async def health_check(self) -> ProviderHealth:
        def _run() -> ProviderHealth:
            # A cheap authenticated call proves creds + region resolve.
            self._client("cloudwatch").describe_alarms(MaxRecords=1)
            return ProviderHealth(provider="cloudwatch", ok=True, detail=self._region)

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(provider="cloudwatch", ok=False, detail=str(e)[:200])


__all__ = ["CloudWatchAdapter"]
