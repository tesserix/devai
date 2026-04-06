"""Cost Analyzer Agent — tracks cloud spending and identifies waste.

Monitors GCP billing, identifies idle resources, oversized instances,
and recommends cost optimizations.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Cost Analyst. Analyze cloud resource usage and identify cost optimizations.

Check for:
1. Oversized pods (requesting much more CPU/memory than using)
2. Idle deployments (0 traffic but consuming resources)
3. Orphaned PVCs (persistent volumes with no attached pod)
4. Unnecessary replicas (high availability where single replica is fine)
5. Missing resource limits (pods that could consume unlimited resources)
6. Cost trends vs previous period

Output JSON:
{
    "total_monthly_estimate_usd": 1234.56,
    "potential_savings_usd": 200.00,
    "findings": [
        {
            "severity": "medium",
            "category": "cost",
            "title": "Oversized deployment: homechef-api",
            "current_cost": "requests 2 CPU, using 0.3 CPU",
            "recommendation": "Reduce CPU request to 500m",
            "estimated_savings_usd": 45.00
        }
    ]
}"""


class CostAnalyzerAgent:
    """Analyzes cloud costs and recommends optimizations."""

    name = "cost_analyzer"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.groq = GroqProvider(config)
        self.k8s = K8sToolExecutor()

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Analyze resource usage and estimate costs."""
        resource_data: list[str] = []

        for ns in namespaces:
            usage = await self.k8s.execute("k8s_get_resource_usage", {"namespace": ns})
            deps = await self.k8s.execute("k8s_get_deployments", {"namespace": ns})
            resource_data.append(f"## {ns}\nUsage: {usage}\nDeployments: {deps}")

        # Check for orphaned PVCs
        pvc_data = await self.k8s._kubectl("get", "pvc", "--all-namespaces", "-o", "json")

        combined = "\n\n".join(resource_data)
        if pvc_data:
            combined += f"\n\n## PVCs\n{pvc_data[:3000]}"

        analysis = await self.groq.generate(
            prompt=f"Analyze resource usage and identify cost optimizations:\n\n{combined[:8000]}",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(analysis)
        except json.JSONDecodeError:
            return {"findings": [], "raw": analysis[:2000]}
