"""Performance Monitor Agent — tracks latency, throughput, error rates.

Checks Prometheus metrics (if available) or derives performance data
from pod resource usage and log patterns.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Principal SRE Performance Engineer. Perform deep performance analysis on a production Kubernetes cluster.

## Analysis Areas

1. **Latency Analysis**
   - P50, P95, P99 latencies — flag P99 > 500ms as warning, > 1s as critical
   - Identify slowest endpoints/services
   - Detect latency trends (increasing over time = degradation)

2. **Throughput Analysis**
   - Requests per second (RPS) per service
   - Compare current RPS to baseline (detect anomalies: sudden drops or spikes)
   - Calculate capacity headroom (current RPS vs max tested RPS)
   - Traffic distribution across services

3. **Error Rate Analysis**
   - 5xx error rates per service — flag > 0.1% as warning, > 1% as critical
   - 4xx spike detection (may indicate broken clients or API changes)
   - Error rate trends (increasing = regression)

4. **Resource Efficiency**
   - CPU utilization vs requests (cost per request)
   - Memory utilization vs limits (OOM risk assessment)
   - Pod count vs traffic (over-provisioning detection)
   - CPU throttling detection (requested < used → throttled)

5. **Database & Dependencies**
   - Slow query patterns from logs (> 100ms queries)
   - Connection pool exhaustion signals
   - Redis/cache hit ratios (if in logs)
   - External API dependency latency

6. **Restart & Stability**
   - Pod restart frequency — flag any restart > 0 in last hour
   - OOMKilled events
   - Crash loop detection (> 3 restarts in 15 min)
   - Readiness probe failures

## Output Format

Output JSON with actionable fix recommendations:
{
    "overall_status": "healthy|degraded|critical",
    "summary": "2-3 sentence executive summary of cluster performance",
    "findings": [
        {
            "severity": "critical|high|medium|low",
            "category": "performance",
            "app": "service-name",
            "namespace": "namespace",
            "title": "Clear, specific finding title",
            "metric": "metric_name",
            "current_value": "measured value",
            "threshold": "expected threshold",
            "evidence": "specific data supporting this finding",
            "recommendation": "Step-by-step fix instructions",
            "fix_commands": ["kubectl command 1", "kubectl command 2"]
        }
    ]
}

Be specific and actionable. Every finding MUST include a concrete recommendation with kubectl commands or config changes."""


class PerfMonitorAgent:
    """Monitors application performance metrics."""

    name = "perf_monitor"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.openai = OpenAIProvider(config)
        self.k8s = K8sToolExecutor()
        self._prometheus_url = (
            getattr(config, "prometheus_url", None)
            or os.environ.get("DEVAI_PROMETHEUS_URL")
            or "http://prometheus-server.monitoring.svc.cluster.local:80"
        )

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Deep performance analysis across monitored namespaces."""
        perf_data: list[str] = []

        # Try Prometheus queries first
        prom_available = await self._check_prometheus()

        if prom_available:
            queries = {
                "error_rate_5xx": 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (namespace, service) / sum(rate(http_requests_total[5m])) by (namespace, service)',
                "error_rate_4xx": 'sum(rate(http_requests_total{status=~"4.."}[5m])) by (namespace, service) / sum(rate(http_requests_total[5m])) by (namespace, service)',
                "p50_latency": "histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, namespace, service))",
                "p95_latency": "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, namespace, service))",
                "p99_latency": "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, namespace, service))",
                "request_rate_rps": "sum(rate(http_requests_total[5m])) by (namespace, service)",
                "request_rate_1h": "sum(rate(http_requests_total[1h])) by (namespace, service)",
                "pod_restarts_1h": "sum(increase(kube_pod_container_status_restarts_total[1h])) by (namespace, pod)",
                "cpu_throttling": "sum(rate(container_cpu_cfs_throttled_periods_total[5m])) by (namespace, pod) / sum(rate(container_cpu_cfs_periods_total[5m])) by (namespace, pod)",
                "memory_usage_pct": "sum(container_memory_working_set_bytes) by (namespace, pod) / sum(container_spec_memory_limit_bytes) by (namespace, pod)",
            }

            for name, query in queries.items():
                result = await self._prom_query(query)
                if result:
                    perf_data.append(f"## {name}\n{json.dumps(result[:20], indent=2)}")

        # Always get resource usage and pod status from kubectl
        for ns in namespaces:
            usage = await self.k8s.execute("k8s_get_resource_usage", {"namespace": ns})
            perf_data.append(f"## Resource Usage: {ns}\n{usage}")

            # Pod status for restarts and OOMKilled detection
            pod_status = await self.k8s.execute("k8s_get_pod_status", {"namespace": ns})
            perf_data.append(f"## Pod Status: {ns}\n{pod_status}")

            # HPA status for autoscaling signals
            hpa = await self.k8s.execute("k8s_get_hpa_status", {"namespace": ns})
            if hpa and "No HPA" not in str(hpa):
                perf_data.append(f"## HPA Status: {ns}\n{hpa}")

            # Deployment info for replica counts and images
            deployments = await self.k8s.execute("k8s_get_deployments", {"namespace": ns})
            perf_data.append(f"## Deployments: {ns}\n{deployments}")

            # Get error logs from all pods (performance signals)
            pods_raw = await self.k8s._kubectl("get", "pods", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}")
            if pods_raw:
                for pod_name in pods_raw.split()[:5]:
                    logs = await self.k8s.execute(
                        "k8s_get_pod_logs",
                        {
                            "namespace": ns,
                            "pod_name": pod_name,
                            "lines": 50,
                            "errors_only": True,
                        },
                    )
                    if logs and "No logs" not in str(logs):
                        perf_data.append(f"## Error Logs: {ns}/{pod_name}\n{logs}")

            # Check events for OOMKilled, BackOff, etc.
            events = await self.k8s.execute("k8s_get_events", {"namespace": ns})
            if events:
                perf_data.append(f"## Events: {ns}\n{events}")

        combined = "\n\n".join(perf_data)

        analysis = await self.openai.generate(
            prompt=f"Perform a deep performance analysis on this production cluster. "
            f"Check throughput, latency, error rates, resource efficiency, and stability. "
            f"Provide specific fix recommendations with kubectl commands.\n\n{combined[:12000]}",
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
