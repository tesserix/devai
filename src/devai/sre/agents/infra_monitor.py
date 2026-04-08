"""Infrastructure Monitor Agent — watches K8s cluster health.

Checks: node status, pod health, deployment replicas, resource pressure,
cert expiry, HPA activity, and Kubernetes events.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Infrastructure Monitor. Your job is to check the health of the Kubernetes cluster.

Run these checks systematically:
1. k8s_get_cluster_health — node status, capacity
2. k8s_get_pod_status — for each monitored namespace
3. k8s_get_events — Warning events
4. k8s_get_deployments — replica health
5. k8s_check_certificates — TLS cert expiry
6. k8s_get_hpa_status — autoscaler activity

For each issue found, classify:
- CRITICAL: Node NotReady, multiple pods crashlooping, certs expiring < 7 days
- HIGH: Pods pending > 5 min, deployment replicas mismatch, high restart counts
- MEDIUM: Warning events, HPA at max, resource pressure
- LOW: Informational, minor config issues

Output a structured JSON report:
{
    "status": "healthy|degraded|critical",
    "findings": [
        {"severity": "critical", "category": "availability", "title": "...", "description": "...", "evidence": {...}}
    ]
}"""


class InfraMonitorAgent:
    """Monitors Kubernetes cluster infrastructure health."""

    name = "infra_monitor"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.groq = GroqProvider(config)
        self.k8s = K8sToolExecutor()

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Run infrastructure health checks across all monitored namespaces."""
        # Gather data from all namespaces
        cluster_health = await self.k8s.execute("k8s_get_cluster_health", {})
        events = await self.k8s.execute("k8s_get_events", {"event_type": "Warning"})

        namespace_data = []
        for ns in namespaces:
            pods = await self.k8s.execute("k8s_get_pod_status", {"namespace": ns})
            deps = await self.k8s.execute("k8s_get_deployments", {"namespace": ns})
            hpa = await self.k8s.execute("k8s_get_hpa_status", {"namespace": ns})
            namespace_data.append(f"## Namespace: {ns}\nPods: {pods}\nDeployments: {deps}\nHPA: {hpa}")

        certs = await self.k8s.execute("k8s_check_certificates", {})

        # Use Groq for fast analysis
        combined = f"""## Cluster Health
{cluster_health}

## Warning Events
{events}

## Certificates
{certs}

{"".join(namespace_data)}"""

        analysis = await self.groq.generate(
            prompt=f"Analyze this Kubernetes cluster state and identify issues:\n\n{combined[:8000]}",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        import json

        try:
            return json.loads(analysis)
        except json.JSONDecodeError:
            return {"status": "unknown", "findings": [], "raw": analysis[:2000]}
