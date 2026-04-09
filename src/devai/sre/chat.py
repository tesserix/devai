"""SRE Chat Agent — conversational interface for cluster operations.

Provides an interactive chat for SRE engineers to:
- Query cluster health, incidents, metrics, costs
- Trigger targeted scans on specific namespaces
- Ask the SRE agents to investigate specific issues
- Get fix recommendations for incidents
- Inject instructions for the next autonomous scan
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from devai.providers.openai_provider import OpenAIProvider
from devai.sre.tools.gcp_tools import GCPToolExecutor
from devai.sre.tools.k8s_tools import K8sToolExecutor
from devai.sre.tools.prometheus_tools import PrometheusToolExecutor

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.services.database import Database

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the DevAI SRE Assistant — an expert Site Reliability Engineer with full access to:

1. **Kubernetes cluster** (GKE `tesseract-prod-in-gke` in `asia-south1`) — pods, deployments, services, resource usage, events, logs, HPA, certificates
2. **Google Cloud Platform** — Cloud Monitoring metrics, Cloud Logging, Cloud SQL instances, Storage buckets, Compute instances, quotas, Recommender insights
3. **Prometheus** — PromQL queries, alerts, scrape target health
4. **SRE database** — incidents, health checks, cost reports, scan history

When answering:
- Always fetch real data using tools — never guess cluster or cloud state
- Format responses with markdown tables, bullet points, code blocks
- For kubectl commands, show the exact command the user could run
- For incidents, include severity, category, MTTR, and fix recommendations
- For performance issues, include Prometheus metrics with thresholds and trends
- For GCP resources, include project, region, and cost context
- Be specific — cite pod names, namespaces, instance names, timestamps

You are connected to GCP project `tesseracthub-480811` and GKE cluster `tesseract-prod-in-gke`.
"""


class SREChatAgent:
    """Conversational SRE assistant with kubectl, GCP, Prometheus, and database access."""

    def __init__(self, config: Settings, database: Database | None = None) -> None:
        self.config = config
        self.db = database
        self.llm = OpenAIProvider(config)
        self.k8s = K8sToolExecutor()
        self.gcp = GCPToolExecutor()
        self.prom = PrometheusToolExecutor()
        self._conversations: dict[str, list[dict[str, str]]] = {}

    async def chat(self, message: str, session_id: str = "default") -> str:
        """Process a user message and return a response."""
        if session_id not in self._conversations:
            self._conversations[session_id] = []

        history = self._conversations[session_id]
        history.append({"role": "user", "content": message})

        # Gather context by running tools based on the message
        context = await self._gather_context(message)

        # Build prompt with conversation history
        history_text = ""
        for msg in history[-10:]:  # Last 10 messages
            role = "User" if msg["role"] == "user" else "Assistant"
            history_text += f"\n**{role}:** {msg['content']}\n"

        prompt = f"""## Conversation History
{history_text}

## Live Cluster Data
{context}

## Current User Message
{message}

Respond to the user's message using the live cluster data above. Be specific and actionable."""

        response = await self.llm.generate(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            max_tokens=4096,
        )

        history.append({"role": "assistant", "content": response})

        # Trim history
        if len(history) > 30:
            self._conversations[session_id] = history[-30:]

        return response

    async def _gather_context(self, message: str) -> str:
        """Fetch relevant cluster data based on the user's message."""
        msg_lower = message.lower()
        context_parts: list[str] = []

        # Always include a quick cluster health summary
        try:
            if self.db:
                rows = await self.db.pool.fetch("SELECT * FROM v_sre_cluster_health")
                if rows:
                    context_parts.append(
                        "### Cluster Health\n" + json.dumps([dict(r) for r in rows], indent=2, default=str)
                    )
        except Exception:
            pass

        # Check for namespace-specific queries
        namespaces_to_check: list[str] = []
        known_ns = ["devai", "homechef", "marketplace", "istio-system", "argocd", "postgresql", "redis", "monitoring"]
        for ns in known_ns:
            if ns in msg_lower:
                namespaces_to_check.append(ns)

        # Pod/deployment queries
        if any(kw in msg_lower for kw in ["pod", "crash", "restart", "deploy", "status", "health", "down", "failing"]):
            for ns in namespaces_to_check or ["default"]:
                pods = await self.k8s.execute("k8s_get_pod_status", {"namespace": ns})
                context_parts.append(f"### Pods in {ns}\n{pods}")

        # Log queries
        if any(kw in msg_lower for kw in ["log", "error", "exception", "trace", "debug"]):
            for ns in namespaces_to_check or ["default"]:
                pods_raw = await self.k8s._kubectl("get", "pods", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}")
                if pods_raw:
                    for pod in pods_raw.split()[:3]:
                        logs = await self.k8s.execute(
                            "k8s_get_pod_logs", {"namespace": ns, "pod_name": pod, "lines": 50, "errors_only": True}
                        )
                        if logs and "No logs" not in logs:
                            context_parts.append(f"### Logs: {ns}/{pod}\n{logs[:3000]}")

        # Resource usage queries
        if any(kw in msg_lower for kw in ["cpu", "memory", "resource", "usage", "capacity", "node", "throughput"]):
            cluster = await self.k8s.execute("k8s_get_cluster_health", {})
            context_parts.append(f"### Cluster Nodes\n{cluster}")
            for ns in namespaces_to_check or ["default"]:
                usage = await self.k8s.execute("k8s_get_resource_usage", {"namespace": ns})
                context_parts.append(f"### Resource Usage: {ns}\n{usage}")

        # Events
        if any(kw in msg_lower for kw in ["event", "warning", "issue", "alert"]):
            for ns in namespaces_to_check or ["default"]:
                events = await self.k8s.execute("k8s_get_events", {"namespace": ns})
                context_parts.append(f"### Events: {ns}\n{events}")

        # HPA
        if any(kw in msg_lower for kw in ["scale", "hpa", "autoscal", "replica"]):
            for ns in namespaces_to_check or ["default"]:
                hpa = await self.k8s.execute("k8s_get_hpa_status", {"namespace": ns})
                context_parts.append(f"### HPA: {ns}\n{hpa}")

        # Incidents from DB
        if any(kw in msg_lower for kw in ["incident", "finding", "issue", "problem", "fix", "resolve", "mttr"]):
            try:
                if self.db:
                    rows = await self.db.pool.fetch(
                        "SELECT id, severity, category, title, description, status, detected_by, created_at FROM sre_incidents ORDER BY created_at DESC LIMIT 15"
                    )
                    if rows:
                        context_parts.append(
                            "### Recent Incidents\n" + json.dumps([dict(r) for r in rows], indent=2, default=str)
                        )
            except Exception:
                pass

        # Cost data
        if any(kw in msg_lower for kw in ["cost", "spend", "bill", "waste", "saving", "expensive"]):
            try:
                if self.db:
                    rows = await self.db.pool.fetch("SELECT * FROM sre_cost_reports ORDER BY report_date DESC LIMIT 7")
                    if rows:
                        context_parts.append(
                            "### Cost Reports (last 7 days)\n"
                            + json.dumps([dict(r) for r in rows], indent=2, default=str)
                        )
            except Exception:
                pass

        # Deployments
        if any(kw in msg_lower for kw in ["deploy", "image", "version", "rollout", "argo"]):
            for ns in namespaces_to_check or ["default"]:
                deps = await self.k8s.execute("k8s_get_deployments", {"namespace": ns})
                context_parts.append(f"### Deployments: {ns}\n{deps}")

        # GCP resources
        if any(kw in msg_lower for kw in ["bucket", "storage", "gcs"]):
            data = await self.gcp.execute("gcp_get_storage_buckets", {})
            context_parts.append(f"### Cloud Storage\n{data}")

        if any(kw in msg_lower for kw in ["sql", "database", "postgres", "cloudsql", "db instance"]):
            data = await self.gcp.execute("gcp_get_sql_instances", {})
            context_parts.append(f"### Cloud SQL\n{data}")

        if any(kw in msg_lower for kw in ["vm", "instance", "compute", "machine"]):
            data = await self.gcp.execute("gcp_get_compute_instances", {})
            context_parts.append(f"### Compute Instances\n{data}")

        if any(kw in msg_lower for kw in ["quota", "limit", "capacity"]):
            data = await self.gcp.execute("gcp_get_quotas", {})
            context_parts.append(f"### Quotas\n{data}")

        if any(kw in msg_lower for kw in ["recommend", "advisor", "optimization", "optimiz", "suggestion"]):
            data = await self.gcp.execute("gcp_get_recommendations", {})
            context_parts.append(f"### GCP Recommendations\n{data}")

        if any(kw in msg_lower for kw in ["gcp", "project", "cloud", "service"]):
            data = await self.gcp.execute("gcp_get_project_info", {})
            context_parts.append(f"### GCP Project\n{data}")

        if any(kw in msg_lower for kw in ["monitor", "metric", "cloud monitor"]):
            data = await self.gcp.execute(
                "gcp_get_monitoring_metrics",
                {"metric_type": "kubernetes.io/node/cpu/allocatable_utilization", "minutes": 30},
            )
            context_parts.append(f"### GCP Monitoring — Node CPU\n{data}")

        if any(kw in msg_lower for kw in ["cloud log", "gcp log", "stackdriver"]):
            data = await self.gcp.execute(
                "gcp_get_logs",
                {"filter_str": "severity>=ERROR", "minutes": 60, "max_entries": 20},
            )
            context_parts.append(f"### GCP Error Logs\n{data}")

        # Prometheus
        if any(kw in msg_lower for kw in ["prometheus", "promql", "prom", "alert", "scrape", "target"]):
            if "alert" in msg_lower:
                data = await self.prom.execute("prom_get_alerts", {})
                context_parts.append(f"### Prometheus Alerts\n{data}")
            elif "target" in msg_lower or "scrape" in msg_lower:
                data = await self.prom.execute("prom_get_targets", {})
                context_parts.append(f"### Prometheus Targets\n{data}")
            else:
                data = await self.prom.execute(
                    "prom_query",
                    {"query": "up"},
                )
                context_parts.append(f"### Prometheus Status\n{data}")

        if any(kw in msg_lower for kw in ["latency", "error rate", "throughput", "request rate", "p99", "p95"]):
            queries = {
                "error_rate": 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (namespace)',
                "request_rate": "sum(rate(http_requests_total[5m])) by (namespace)",
            }
            for label, query in queries.items():
                data = await self.prom.execute("prom_query", {"query": query})
                context_parts.append(f"### Prometheus: {label}\n{data}")

        # If nothing specific matched, get a general overview
        if not context_parts:
            cluster = await self.k8s.execute("k8s_get_cluster_health", {})
            context_parts.append(f"### Cluster Overview\n{cluster}")
            try:
                if self.db:
                    rows = await self.db.pool.fetch(
                        "SELECT id, severity, category, title, status, created_at FROM sre_incidents WHERE status = 'open' ORDER BY created_at DESC LIMIT 10"
                    )
                    if rows:
                        context_parts.append(
                            "### Open Incidents\n" + json.dumps([dict(r) for r in rows], indent=2, default=str)
                        )
            except Exception:
                pass

        return "\n\n".join(context_parts) if context_parts else "(No cluster data available)"

    def clear_session(self, session_id: str) -> None:
        self._conversations.pop(session_id, None)
