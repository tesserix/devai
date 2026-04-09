"""Prometheus monitoring tools for SRE agents.

Queries Prometheus and Alertmanager via HTTP API for:
- Instant PromQL queries
- Range queries over time windows
- Active alerts
- Scrape target health
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False
    logger.debug("httpx not installed — Prometheus tools unavailable")


PROMETHEUS_TOOLS: list[dict[str, Any]] = [
    {
        "name": "prom_query",
        "description": (
            "Execute an instant PromQL query against the Prometheus server. "
            "Returns the current value of the expression. "
            "Examples: 'up', 'rate(http_requests_total[5m])', "
            "'kube_pod_container_status_restarts_total > 5'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL expression to evaluate.",
                },
                "time": {
                    "type": "string",
                    "description": ("Evaluation timestamp (RFC3339 or Unix). Defaults to current server time."),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "prom_query_range",
        "description": (
            "Execute a range PromQL query over a time window. "
            "Returns a matrix of values sampled at the given step interval. "
            "Useful for graphing trends like CPU over time or error rate spikes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL expression to evaluate over the range.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Look-back window in minutes (default 30, max 1440).",
                },
                "step": {
                    "type": "string",
                    "description": "Query resolution step (default '60s'). E.g. '15s', '1m', '5m'.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "prom_get_alerts",
        "description": (
            "Get currently active alerts from Prometheus and Alertmanager. "
            "Returns firing and pending alerts with labels, annotations, and durations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Filter by alert state: 'firing', 'pending', or 'inactive'. Empty for all active.",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter by severity label: 'critical', 'warning', 'info'. Empty for all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "prom_get_targets",
        "description": (
            "Get scrape target health from Prometheus. "
            "Shows which targets are up/down, their last scrape time, and any errors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Filter by target state: 'active', 'dropped', or 'any' (default 'active').",
                },
            },
            "required": [],
        },
    },
]


class PrometheusToolExecutor:
    """Executes Prometheus monitoring tools via the Prometheus HTTP API."""

    def __init__(self) -> None:
        self._prom_url = os.environ.get(
            "DEVAI_PROMETHEUS_URL",
            "http://prometheus-server.monitoring.svc.cluster.local:80",
        ).rstrip("/")
        # Alertmanager is typically on a separate service
        self._alertmanager_url = os.environ.get(
            "DEVAI_ALERTMANAGER_URL",
            "http://alertmanager.monitoring.svc.cluster.local:9093",
        ).rstrip("/")

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if not _HAS_HTTPX:
            return "httpx is not installed. Install with: pip install httpx"

        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            result = await handler(tool_input)
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2, default=str)
        except httpx.ConnectError:
            return (
                f"Cannot connect to Prometheus at {self._prom_url}. "
                "Ensure the Prometheus server is running and reachable."
            )
        except httpx.TimeoutException:
            return f"Prometheus query timed out. The server at {self._prom_url} may be overloaded."
        except Exception as exc:
            logger.warning("Prometheus tool %s failed: %s", tool_name, exc, exc_info=True)
            return f"Error executing {tool_name}: {exc}"

    # ------------------------------------------------------------------
    # Instant Query
    # ------------------------------------------------------------------

    async def _handle_prom_query(self, inp: dict[str, Any]) -> str:
        query = inp["query"]
        params: dict[str, str] = {"query": query}

        if inp.get("time"):
            params["time"] = inp["time"]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._prom_url}/api/v1/query", params=params)

        data = resp.json()

        if data.get("status") != "success":
            error_msg = data.get("error", "Unknown error")
            error_type = data.get("errorType", "")
            return f"Prometheus query failed ({error_type}): {error_msg}\nQuery: {query}"

        result = data.get("data", {})
        result_type = result.get("resultType", "unknown")
        results = result.get("result", [])

        if not results:
            return f"Query returned no results.\nQuery: {query}\nResult type: {result_type}"

        lines = [
            f"Query: {query}",
            f"Result type: {result_type}",
            f"Series count: {len(results)}",
            "",
        ]

        for series in results[:50]:  # Cap output
            metric = series.get("metric", {})
            label_str = self._format_labels(metric)

            if result_type == "vector":
                timestamp, value = series.get("value", [0, ""])
                ts_str = datetime.fromtimestamp(float(timestamp), tz=UTC).strftime("%H:%M:%S")
                lines.append(f"  {label_str}")
                lines.append(f"    Value: {value} (at {ts_str})")

            elif result_type == "scalar":
                timestamp, value = series
                lines.append(f"  Scalar: {value}")

            elif result_type == "string":
                timestamp, value = series
                lines.append(f"  String: {value}")

            lines.append("")

        if len(results) > 50:
            lines.append(f"  ... and {len(results) - 50} more series (truncated)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Range Query
    # ------------------------------------------------------------------

    async def _handle_prom_query_range(self, inp: dict[str, Any]) -> str:
        query = inp["query"]
        minutes = min(inp.get("minutes", 30), 1440)
        step = inp.get("step", "60s")

        now = datetime.now(UTC)
        start = now - timedelta(minutes=minutes)

        params = {
            "query": query,
            "start": start.isoformat(),
            "end": now.isoformat(),
            "step": step,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(f"{self._prom_url}/api/v1/query_range", params=params)

        data = resp.json()

        if data.get("status") != "success":
            error_msg = data.get("error", "Unknown error")
            error_type = data.get("errorType", "")
            return f"Prometheus range query failed ({error_type}): {error_msg}\nQuery: {query}"

        result = data.get("data", {})
        results = result.get("result", [])

        if not results:
            return f"Range query returned no results.\nQuery: {query}\nWindow: {minutes}m, Step: {step}"

        lines = [
            f"Query: {query}",
            f"Window: last {minutes} minutes, Step: {step}",
            f"Series count: {len(results)}",
            "",
        ]

        for series in results[:20]:  # Cap series
            metric = series.get("metric", {})
            label_str = self._format_labels(metric)
            values = series.get("values", [])

            lines.append(f"  {label_str}")
            lines.append(f"    Data points: {len(values)}")

            if values:
                # Show summary statistics
                import contextlib

                numeric_values = []
                for _ts, val in values:
                    with contextlib.suppress(ValueError, TypeError):
                        numeric_values.append(float(val))

                if numeric_values:
                    lines.append(f"    Min: {min(numeric_values):.4g}")
                    lines.append(f"    Max: {max(numeric_values):.4g}")
                    lines.append(f"    Avg: {sum(numeric_values) / len(numeric_values):.4g}")
                    lines.append(f"    Latest: {numeric_values[-1]:.4g}")

                # Show first and last few data points
                sample_points = values[:3] + (["..."] if len(values) > 6 else []) + values[-3:]
                for point in sample_points:
                    if point == "...":
                        lines.append("      ...")
                    else:
                        ts, val = point
                        ts_str = datetime.fromtimestamp(float(ts), tz=UTC).strftime("%H:%M:%S")
                        lines.append(f"      [{ts_str}] {val}")

            lines.append("")

        if len(results) > 20:
            lines.append(f"  ... and {len(results) - 20} more series (truncated)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def _handle_prom_get_alerts(self, inp: dict[str, Any]) -> str:
        state_filter = inp.get("state", "")
        severity_filter = inp.get("severity", "")

        # Try Prometheus alerts endpoint first
        alerts = await self._fetch_prometheus_alerts()

        # Also try Alertmanager for richer alert data
        am_alerts = await self._fetch_alertmanager_alerts()

        # Merge — Alertmanager alerts take precedence for firing alerts
        all_alerts = am_alerts if am_alerts else alerts

        if not all_alerts:
            return "No active alerts found."

        # Apply filters
        if state_filter:
            all_alerts = [a for a in all_alerts if a.get("state", "").lower() == state_filter.lower()]

        if severity_filter:
            all_alerts = [
                a for a in all_alerts if a.get("labels", {}).get("severity", "").lower() == severity_filter.lower()
            ]

        if not all_alerts:
            filters = []
            if state_filter:
                filters.append(f"state={state_filter}")
            if severity_filter:
                filters.append(f"severity={severity_filter}")
            return f"No alerts matching filters: {', '.join(filters)}"

        # Sort: critical first, then warning, then by name
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        all_alerts.sort(
            key=lambda a: (
                severity_order.get(a.get("labels", {}).get("severity", "info"), 3),
                a.get("labels", {}).get("alertname", ""),
            )
        )

        lines = [
            f"Active Alerts: {len(all_alerts)}",
            "",
        ]

        for alert in all_alerts[:50]:
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            state = alert.get("state", "unknown")
            alertname = labels.get("alertname", "unknown")
            severity = labels.get("severity", "unset")
            namespace = labels.get("namespace", "")

            lines.append(f"  [{severity.upper()}] {alertname} ({state})")
            if namespace:
                lines.append(f"    Namespace: {namespace}")
            if annotations.get("summary"):
                lines.append(f"    Summary: {annotations['summary']}")
            if annotations.get("description"):
                desc = annotations["description"][:300]
                lines.append(f"    Description: {desc}")
            if alert.get("activeAt"):
                lines.append(f"    Active since: {alert['activeAt']}")
            elif alert.get("startsAt"):
                lines.append(f"    Started: {alert['startsAt']}")

            # Show relevant labels (skip common ones)
            extra_labels = {
                k: v for k, v in labels.items() if k not in ("alertname", "severity", "namespace", "__name__")
            }
            if extra_labels:
                label_str = ", ".join(f"{k}={v}" for k, v in list(extra_labels.items())[:5])
                lines.append(f"    Labels: {label_str}")

            lines.append("")

        if len(all_alerts) > 50:
            lines.append(f"  ... and {len(all_alerts) - 50} more alerts (truncated)")

        return "\n".join(lines)

    async def _fetch_prometheus_alerts(self) -> list[dict[str, Any]]:
        """Fetch alerts from Prometheus /api/v1/alerts."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self._prom_url}/api/v1/alerts")

            data = resp.json()
            if data.get("status") != "success":
                return []

            alerts = data.get("data", {}).get("alerts", [])
            return [a for a in alerts if a.get("state") in ("firing", "pending")]
        except Exception as exc:
            logger.debug("Failed to fetch Prometheus alerts: %s", exc)
            return []

    async def _fetch_alertmanager_alerts(self) -> list[dict[str, Any]]:
        """Fetch alerts from Alertmanager /api/v2/alerts."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self._alertmanager_url}/api/v2/alerts")

            if resp.status_code != 200:
                return []

            raw_alerts = resp.json()
            alerts = []
            for a in raw_alerts:
                status = a.get("status", {})
                if status.get("state") == "suppressed":
                    continue
                alerts.append(
                    {
                        "state": status.get("state", "active"),
                        "labels": a.get("labels", {}),
                        "annotations": a.get("annotations", {}),
                        "startsAt": a.get("startsAt", ""),
                        "endsAt": a.get("endsAt", ""),
                        "fingerprint": a.get("fingerprint", ""),
                    }
                )

            return alerts
        except Exception as exc:
            logger.debug("Failed to fetch Alertmanager alerts: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    async def _handle_prom_get_targets(self, inp: dict[str, Any]) -> str:
        state_filter = inp.get("state", "active")

        params = {}
        if state_filter and state_filter != "any":
            params["state"] = state_filter

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self._prom_url}/api/v1/targets", params=params)

        data = resp.json()

        if data.get("status") != "success":
            error_msg = data.get("error", "Unknown error")
            return f"Failed to get targets: {error_msg}"

        targets_data = data.get("data", {})
        active_targets = targets_data.get("activeTargets", [])
        dropped_targets = targets_data.get("droppedTargets", [])

        # Count by health
        health_counts: dict[str, int] = {}
        for t in active_targets:
            health = t.get("health", "unknown")
            health_counts[health] = health_counts.get(health, 0) + 1

        lines = [
            f"Active Targets: {len(active_targets)}",
            f"Dropped Targets: {len(dropped_targets)}",
            f"Health: {', '.join(f'{k}={v}' for k, v in sorted(health_counts.items()))}",
            "",
        ]

        # Show unhealthy targets first
        unhealthy = [t for t in active_targets if t.get("health") != "up"]
        healthy = [t for t in active_targets if t.get("health") == "up"]

        if unhealthy:
            lines.append("DOWN / UNHEALTHY targets:")
            for t in unhealthy[:30]:
                job = t.get("labels", {}).get("job", "unknown")
                instance = t.get("labels", {}).get("instance", "unknown")
                health = t.get("health", "unknown")
                error = t.get("lastError", "")
                last_scrape = t.get("lastScrape", "")

                lines.append(f"  [{health.upper()}] {job} -> {instance}")
                if error:
                    lines.append(f"    Error: {error[:200]}")
                if last_scrape:
                    lines.append(f"    Last scrape: {last_scrape}")
                lines.append("")

        # Summarize healthy targets by job
        if healthy:
            job_counts: dict[str, int] = {}
            for t in healthy:
                job = t.get("labels", {}).get("job", "unknown")
                job_counts[job] = job_counts.get(job, 0) + 1

            lines.append("Healthy targets by job:")
            for job, count in sorted(job_counts.items()):
                lines.append(f"  {job}: {count} targets (up)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_labels(metric: dict[str, str]) -> str:
        """Format metric labels into a readable string."""
        if not metric:
            return "{}"

        name = metric.get("__name__", "")
        other_labels = {k: v for k, v in metric.items() if k != "__name__"}

        if name and other_labels:
            label_pairs = ", ".join(f'{k}="{v}"' for k, v in list(other_labels.items())[:8])
            result = f"{name}{{{label_pairs}}}"
            if len(other_labels) > 8:
                result += f" (+{len(other_labels) - 8} labels)"
            return result
        elif name:
            return name
        elif other_labels:
            label_pairs = ", ".join(f'{k}="{v}"' for k, v in list(other_labels.items())[:8])
            return f"{{{label_pairs}}}"
        return "{}"
