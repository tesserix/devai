"""Capacity Planner Agent — predicts resource needs based on trends.

Analyzes historical metrics and growth patterns to forecast:
- When nodes will need scaling
- Which apps are approaching resource limits
- Storage growth and PVC usage trends
- Traffic growth patterns
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Capacity Planner. Analyze current resource usage and predict future needs.

Evaluate:
1. Current CPU/memory utilization vs capacity
2. Storage usage and growth trends
3. Pod density per node (are we running out of pod slots?)
4. Traffic patterns and seasonality
5. Time until resource exhaustion at current growth rate

Output JSON:
{
    "cluster_utilization": {
        "cpu_pct": 65.0,
        "memory_pct": 78.0,
        "pod_pct": 45.0,
        "storage_pct": 52.0
    },
    "forecasts": [
        {
            "resource": "memory",
            "current_usage_pct": 78.0,
            "days_until_80pct": 14,
            "days_until_90pct": 45,
            "recommendation": "Add 1 node or increase node pool memory by 50%"
        }
    ],
    "findings": [
        {
            "severity": "medium",
            "category": "capacity",
            "title": "Memory usage trending toward 90% in 45 days",
            "recommendation": "Scale node pool before next month"
        }
    ]
}"""


class CapacityPlannerAgent:
    """Forecasts resource needs and capacity limits."""

    name = "capacity_planner"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.openai = OpenAIProvider(config)
        self.k8s = K8sToolExecutor()

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Analyze capacity across cluster."""
        cluster = await self.k8s.execute("k8s_get_cluster_health", {})

        ns_usage: list[str] = []
        for ns in namespaces:
            usage = await self.k8s.execute("k8s_get_resource_usage", {"namespace": ns})
            deps = await self.k8s.execute("k8s_get_deployments", {"namespace": ns})
            hpa = await self.k8s.execute("k8s_get_hpa_status", {"namespace": ns})
            ns_usage.append(f"## {ns}\nUsage: {usage}\nDeployments: {deps}\nHPA: {hpa}")

        # PVC usage
        pvcs = await self.k8s._kubectl("get", "pvc", "--all-namespaces", "-o", "json")

        combined = f"## Cluster\n{cluster}\n\n{''.join(ns_usage)}"
        if pvcs:
            combined += f"\n\n## PVCs\n{pvcs[:3000]}"

        analysis = await self.openai.generate(
            prompt=f"Analyze capacity and forecast resource needs:\n\n{combined[:10000]}",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(analysis)
        except json.JSONDecodeError:
            return {"findings": [], "raw": analysis[:2000]}
