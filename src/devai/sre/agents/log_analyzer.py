"""Log Analyzer Agent — parses application logs for errors, patterns, and anomalies.

Scans pod logs across monitored namespaces. Identifies:
- Error spikes and new error patterns
- Unhandled exceptions and stack traces
- Slow query warnings
- Authentication failures
- Connection timeouts
"""

from __future__ import annotations

import logging
from typing import Any

from devai.providers.groq_provider import GroqProvider
from devai.sre.tools.k8s_tools import K8sToolExecutor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Log Analyzer. Analyze application logs to identify issues.

For each pod's error logs, identify:
1. Error patterns (recurring errors, new errors)
2. Stack traces and root causes
3. Performance warnings (slow queries, timeouts)
4. Security events (auth failures, suspicious activity)
5. Resource issues (OOM, disk full)

Classify each finding:
- CRITICAL: Unhandled exceptions causing crashes, data loss
- HIGH: Repeated errors, auth failures, connection issues
- MEDIUM: Slow queries, deprecation warnings
- LOW: Debug noise, known benign errors

Output JSON:
{
    "findings": [
        {
            "severity": "high",
            "category": "error",
            "pod": "pod-name",
            "title": "Repeated database connection timeout",
            "pattern": "the error pattern",
            "count": 42,
            "sample_log": "first occurrence log line"
        }
    ]
}"""


class LogAnalyzerAgent:
    """Analyzes pod logs for errors and anomalies."""

    name = "log_analyzer"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.groq = GroqProvider(config)
        self.k8s = K8sToolExecutor()

    async def run(self, namespaces: list[str]) -> dict[str, Any]:
        """Scan logs across all monitored namespaces."""
        all_logs: list[str] = []

        for ns in namespaces:
            # Get pods in namespace
            pods_data = await self.k8s.execute("k8s_get_pod_status", {"namespace": ns})

            import json

            try:
                pods = json.loads(pods_data) if isinstance(pods_data, str) else pods_data
            except json.JSONDecodeError:
                continue

            pod_list = pods.get("issues", [])  # Focus on unhealthy pods first
            # Also sample healthy pods for hidden errors
            if not pod_list:
                # Get all pod names
                raw = await self.k8s._kubectl("get", "pods", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}")
                if raw:
                    pod_names = raw.split()[:5]  # Sample first 5
                    for pn in pod_names:
                        log_result = await self.k8s.execute(
                            "k8s_get_pod_logs",
                            {
                                "namespace": ns,
                                "pod_name": pn,
                                "lines": 50,
                                "errors_only": True,
                            },
                        )
                        if log_result and "logs" in str(log_result):
                            all_logs.append(f"## Pod: {ns}/{pn}\n{log_result}")
            else:
                for pod_info in pod_list[:10]:
                    pn = pod_info.get("name", "")
                    if pn:
                        log_result = await self.k8s.execute(
                            "k8s_get_pod_logs",
                            {
                                "namespace": ns,
                                "pod_name": pn,
                                "lines": 100,
                                "errors_only": True,
                            },
                        )
                        all_logs.append(f"## Pod: {ns}/{pn}\n{log_result}")

        if not all_logs:
            return {"findings": [], "status": "no_errors"}

        combined = "\n\n".join(all_logs)

        analysis = await self.groq.generate(
            prompt=f"Analyze these application error logs:\n\n{combined[:10000]}",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(analysis)
        except json.JSONDecodeError:
            return {"findings": [], "raw": analysis[:2000]}
