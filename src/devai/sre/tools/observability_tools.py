"""Observability fan-out tools for SRE agents.

A single set of vendor-neutral tools (obs_query_metrics / obs_query_logs /
obs_list_alerts / obs_list_sources) that fan out across whichever
observability providers are configured for the tenant — Prometheus,
Datadog, New Relic, CloudWatch, Azure Monitor, Elasticsearch, Grafana. An
agent passes the providers it's allowed to use; the executor runs only
those that are both allowed AND configured, then merges the results.

This is what lets the SRE "connect to one or many tools and feed data in"
without per-vendor tools or per-vendor prompt knowledge.
"""

from __future__ import annotations

import json
from typing import Any

OBSERVABILITY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "obs_list_sources",
        "description": (
            "List the observability providers currently connected (configured) and which are healthy. "
            "Call this first to learn what backends are available before querying."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "obs_query_metrics",
        "description": (
            "Query metrics across one or many connected observability backends and merge the results. "
            "The query is in each backend's native language (PromQL for prometheus/grafana, Datadog "
            "metric query, NRQL for newrelic, Metrics Insights for cloudwatch, a metric name for azure). "
            "Restrict to specific providers with `providers`; omit to use all connected ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Native metrics query for the target backend(s)."},
                "providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subset of providers to query (e.g. ['prometheus','datadog']). Empty = all connected.",
                },
                "window_seconds": {"type": "integer", "description": "Lookback window in seconds (default 3600)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "obs_query_logs",
        "description": "Search logs across connected backends (datadog, elasticsearch, newrelic, cloudwatch, azure) and merge.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Log search string / native log query."},
                "providers": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "description": "Max entries per provider (default 100)."},
                "window_seconds": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "obs_list_alerts",
        "description": "List currently firing alerts across all connected observability backends, merged.",
        "input_schema": {
            "type": "object",
            "properties": {"providers": {"type": "array", "items": {"type": "string"}}},
            "required": [],
        },
    },
]


class ObservabilityToolExecutor:
    """Executes the obs_* fan-out tools via the observability adapter family.

    Constructed with no args (matches the ToolDispatcher convention). Builds
    a MultiObservabilityAdapter per call from the environment-configured
    providers, filtered to the agent's requested subset.
    """

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        from devai.adapters.observability import MultiObservabilityAdapter, providers_from_env

        if tool_name == "obs_list_sources":
            connected = providers_from_env()
            multi = MultiObservabilityAdapter.from_env()
            health = await multi.health()
            return json.dumps(
                {
                    "connected_providers": connected,
                    "health": [{"provider": h.provider, "ok": h.ok, "detail": h.detail} for h in health],
                },
                default=str,
            )

        providers = tool_input.get("providers") or None
        multi = MultiObservabilityAdapter.from_env(allowed=providers)
        if not multi.providers:
            return json.dumps(
                {
                    "result": [],
                    "note": "No observability providers connected for the requested set. "
                    "Configure them in DevAI Settings → Observability.",
                }
            )

        if tool_name == "obs_query_metrics":
            series = await multi.query_metrics(
                tool_input.get("query", ""), window_seconds=int(tool_input.get("window_seconds", 3600))
            )
            return json.dumps(
                {
                    "queried_providers": multi.providers,
                    "series": [
                        {
                            "name": s.name,
                            "provider": s.provider,
                            "latest": s.latest(),
                            "points": s.points[-50:],
                            "labels": s.labels,
                            "unit": s.unit,
                        }
                        for s in series
                    ],
                },
                default=str,
            )

        if tool_name == "obs_query_logs":
            logs = await multi.query_logs(
                tool_input.get("query", ""),
                limit=int(tool_input.get("limit", 100)),
                window_seconds=int(tool_input.get("window_seconds", 3600)),
            )
            return json.dumps(
                {
                    "queried_providers": multi.providers,
                    "logs": [
                        {"ts": e.timestamp, "level": e.level, "service": e.service, "message": e.message, "provider": e.provider}
                        for e in logs
                    ],
                },
                default=str,
            )

        if tool_name == "obs_list_alerts":
            alerts = await multi.get_alerts()
            return json.dumps(
                {
                    "queried_providers": multi.providers,
                    "alerts": [
                        {
                            "title": a.title,
                            "provider": a.provider,
                            "severity": a.severity,
                            "state": a.state,
                            "service": a.service,
                            "description": a.description,
                        }
                        for a in alerts
                    ],
                },
                default=str,
            )

        return f"Unknown tool: {tool_name}"


__all__ = ["OBSERVABILITY_TOOLS", "ObservabilityToolExecutor"]
