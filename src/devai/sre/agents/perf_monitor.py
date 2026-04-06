"""Performance Monitor Agent — tracks latency, throughput, error rates.

Checks Prometheus metrics (if available) or derives performance data
from pod resource usage and log patterns.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Performance Engineer. Analyze application performance metrics.

Evaluate:
1. Response latency (p50, p95, p99) — flag if p99 > 1s
2. Error rates — flag if > 1%
3. Throughput — requests per second trends
4. CPU/memory efficiency — ratio of used vs requested
5. Pod restart frequency — indicator of OOM or crash
6. Database query performance — from logs if available
7. API endpoint health — slowest endpoints

Output JSON:
{
    "overall_status": "healthy|degraded|critical",
    "findings": [
        {
            "severity": "high",
            "category": "performance",
            "app": "homechef-api",
            "title": "P99 latency spike to 3.2s",
            "metric": "http_request_duration_seconds",
            "current_value": "3.2s",
            "threshold": "1.0s",
            "recommendation": "Profile the /api/orders endpoint for N+1 queries"
        }
    ]
}"""


class PerfMonitorAgent:
    """Monitors application performance metrics."""

    name = "perf_monitor"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.groq = GroqProvider(config)
        self.k8s = K8sToolExecutor()
        self._prometheus_url = getattr(config, "prometheus_url", "http://prometheus.monitoring.svc.cluster.local:9090")

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Check performance metrics across monitored namespaces."""
        perf_data: list[str] = []

        # Try Prometheus queries first
        prom_available = await self._check_prometheus()

        if prom_available:
            # Query key metrics
            queries = {
                "error_rate": 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (namespace, service) / sum(rate(http_requests_total[5m])) by (namespace, service)',
                "p99_latency": 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, namespace, service))',
                "request_rate": 'sum(rate(http_requests_total[5m])) by (namespace, service)',
                "pod_restarts": 'sum(increase(kube_pod_container_status_restarts_total[1h])) by (namespace, pod)',
            }

            for name, query in queries.items():
                result = await self._prom_query(query)
                if result:
                    perf_data.append(f"## {name}\n{json.dumps(result[:20], indent=2)}")

        # Always get resource usage from kubectl
        for ns in namespaces:
            usage = await self.k8s.execute("k8s_get_resource_usage", {"namespace": ns})
            perf_data.append(f"## Resource Usage: {ns}\n{usage}")

            # Get error logs as performance signal
            pods_raw = await self.k8s._kubectl("get", "pods", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}")
            if pods_raw:
                for pod_name in pods_raw.split()[:3]:
                    logs = await self.k8s.execute("k8s_get_pod_logs", {
                        "namespace": ns, "pod_name": pod_name, "lines": 30, "errors_only": True,
                    })
                    if logs:
                        perf_data.append(f"## Error Logs: {ns}/{pod_name}\n{logs}")

        combined = "\n\n".join(perf_data)

        analysis = await self.groq.generate(
            prompt=f"Analyze these performance metrics and identify issues:\n\n{combined[:10000]}",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(analysis)
        except json.JSONDecodeError:
            return {"overall_status": "unknown", "findings": [], "raw": analysis[:2000]}

    async def _check_prometheus(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._prometheus_url}/api/v1/status/config")
                return resp.status_code == 200
        except Exception:
            return False

    async def _prom_query(self, query: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self._prometheus_url}/api/v1/query",
                    params={"query": query},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("data", {}).get("result", [])
        except Exception as e:
            logger.debug("Prometheus query failed: %s", e)
        return []
