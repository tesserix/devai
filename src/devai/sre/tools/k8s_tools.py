"""Kubernetes monitoring tools for SRE agents.

Wraps kubectl and the Kubernetes Python client for:
- Cluster health checks (nodes, pods, deployments)
- Resource usage (CPU, memory)
- Pod restarts and crash loops
- Certificate expiry
- PVC usage
- Network policy audit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

K8S_TOOLS: list[dict[str, Any]] = [
    {
        "name": "k8s_get_cluster_health",
        "description": "Get overall cluster health: node status, resource capacity, and system pod health.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to check (empty for cluster-wide)"},
            },
            "required": [],
        },
    },
    {
        "name": "k8s_get_pod_status",
        "description": "Get pod status for a namespace. Identifies crashlooping, pending, or failing pods.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to check"},
                "label_selector": {"type": "string", "description": "Label selector (e.g. app=myapp)"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "k8s_get_resource_usage",
        "description": "Get CPU and memory usage for pods in a namespace using kubectl top.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to check"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "k8s_get_pod_logs",
        "description": "Get recent logs from a pod, filtered for errors and warnings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace"},
                "pod_name": {"type": "string", "description": "Pod name (or partial for label match)"},
                "lines": {"type": "integer", "description": "Number of log lines (default 100)"},
                "errors_only": {"type": "boolean", "description": "Only return error/warning lines"},
            },
            "required": ["namespace", "pod_name"],
        },
    },
    {
        "name": "k8s_get_events",
        "description": "Get recent Kubernetes events, filtered by type (Warning, Normal) and namespace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace (empty for all)"},
                "event_type": {"type": "string", "description": "Warning or Normal"},
            },
            "required": [],
        },
    },
    {
        "name": "k8s_get_deployments",
        "description": "List deployments with replica status, image versions, and resource requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to check"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "k8s_check_certificates",
        "description": "Check TLS certificate expiry for all Ingress/VirtualService endpoints.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace (empty for all)"},
            },
            "required": [],
        },
    },
    {
        "name": "k8s_get_hpa_status",
        "description": "Get HorizontalPodAutoscaler status and scaling activity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace"},
            },
            "required": ["namespace"],
        },
    },
]


class K8sToolExecutor:
    """Executes Kubernetes monitoring tools via kubectl."""

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        result = await handler(tool_input)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)

    async def _handle_k8s_get_cluster_health(self, inp: dict[str, Any]) -> dict[str, Any]:
        nodes = await self._kubectl("get", "nodes", "-o", "json")
        node_list = json.loads(nodes).get("items", []) if nodes else []

        node_health = []
        for node in node_list:
            conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
            allocatable = node.get("status", {}).get("allocatable", {})
            node_health.append(
                {
                    "name": node["metadata"]["name"],
                    "ready": conditions.get("Ready", "Unknown"),
                    "cpu": allocatable.get("cpu", "?"),
                    "memory": allocatable.get("memory", "?"),
                    "pods": allocatable.get("pods", "?"),
                }
            )

        # System pods
        sys_pods = await self._kubectl("get", "pods", "-n", "kube-system", "-o", "json")
        sys_pod_list = json.loads(sys_pods).get("items", []) if sys_pods else []
        unhealthy_sys = [p["metadata"]["name"] for p in sys_pod_list if p.get("status", {}).get("phase") != "Running"]

        return {
            "total_nodes": len(node_health),
            "nodes": node_health,
            "unhealthy_system_pods": unhealthy_sys,
        }

    async def _handle_k8s_get_pod_status(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp["namespace"]
        cmd = ["get", "pods", "-n", ns, "-o", "json"]
        if inp.get("label_selector"):
            cmd.extend(["-l", inp["label_selector"]])

        output = await self._kubectl(*cmd)
        pods = json.loads(output).get("items", []) if output else []

        results = []
        issues = []

        for pod in pods:
            name = pod["metadata"]["name"]
            phase = pod.get("status", {}).get("phase", "Unknown")
            containers = pod.get("status", {}).get("containerStatuses", [])

            restart_count = sum(c.get("restartCount", 0) for c in containers)
            not_ready = [c["name"] for c in containers if not c.get("ready", False)]

            status = "healthy"
            if phase != "Running":
                status = "unhealthy"
            elif restart_count > 5:
                status = "crashlooping"
            elif not_ready:
                status = "degraded"

            entry = {
                "name": name,
                "phase": phase,
                "restarts": restart_count,
                "status": status,
            }
            results.append(entry)

            if status != "healthy":
                issues.append(entry)

        return {
            "namespace": ns,
            "total_pods": len(results),
            "healthy": len([r for r in results if r["status"] == "healthy"]),
            "issues": issues,
        }

    async def _handle_k8s_get_resource_usage(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp["namespace"]
        output = await self._kubectl("top", "pods", "-n", ns, "--no-headers")
        if not output:
            return {"namespace": ns, "pods": [], "error": "kubectl top not available (metrics-server required)"}

        pods = []
        for line in output.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                pods.append(
                    {
                        "name": parts[0],
                        "cpu": parts[1],
                        "memory": parts[2],
                    }
                )

        return {"namespace": ns, "pods": pods}

    async def _handle_k8s_get_pod_logs(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp["namespace"]
        pod = inp["pod_name"]
        lines = inp.get("lines", 100)
        errors_only = inp.get("errors_only", False)

        output = await self._kubectl("logs", pod, "-n", ns, f"--tail={lines}", "--timestamps")
        if not output:
            return {"pod": pod, "logs": "", "error": "No logs available"}

        if errors_only:
            filtered = [
                line
                for line in output.split("\n")
                if any(kw in line.lower() for kw in ["error", "err", "warn", "fatal", "panic", "exception", "fail"])
            ]
            output = "\n".join(filtered[-50:])  # Last 50 error lines

        return {
            "pod": pod,
            "namespace": ns,
            "line_count": len(output.split("\n")),
            "logs": output[:5000],
        }

    async def _handle_k8s_get_events(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp.get("namespace", "")
        # event_type lands in a kubectl --field-selector. _kubectl uses
        # create_subprocess_exec (no shell), so this can't shell-inject, but
        # we still allowlist it so a malformed/agent-supplied value can't build
        # a confusing field-selector. Kubernetes only emits Normal/Warning.
        event_type = inp.get("event_type", "Warning")
        if event_type not in ("Warning", "Normal"):
            event_type = "Warning"

        cmd = ["get", "events", "-o", "json", f"--field-selector=type={event_type}"]
        if ns:
            cmd.extend(["-n", ns])
        else:
            cmd.append("--all-namespaces")

        output = await self._kubectl(*cmd)
        events = json.loads(output).get("items", []) if output else []

        # Sort by last timestamp, take recent
        results = []
        for e in events[-30:]:
            results.append(
                {
                    "namespace": e["metadata"].get("namespace", ""),
                    "reason": e.get("reason", ""),
                    "message": e.get("message", "")[:200],
                    "object": f"{e.get('involvedObject', {}).get('kind', '')}/{e.get('involvedObject', {}).get('name', '')}",
                    "count": e.get("count", 1),
                    "last_seen": e.get("lastTimestamp", ""),
                }
            )

        return {"type": event_type, "total": len(results), "events": results}

    async def _handle_k8s_get_deployments(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp["namespace"]
        output = await self._kubectl("get", "deployments", "-n", ns, "-o", "json")
        deps = json.loads(output).get("items", []) if output else []

        results = []
        for d in deps:
            spec = d.get("spec", {})
            status = d.get("status", {})
            containers = spec.get("template", {}).get("spec", {}).get("containers", [])

            results.append(
                {
                    "name": d["metadata"]["name"],
                    "replicas": spec.get("replicas", 0),
                    "ready": status.get("readyReplicas", 0),
                    "available": status.get("availableReplicas", 0),
                    "images": [c.get("image", "") for c in containers],
                    "cpu_request": sum(
                        self._parse_cpu(c.get("resources", {}).get("requests", {}).get("cpu", "0")) for c in containers
                    ),
                    "memory_request": sum(
                        self._parse_memory(c.get("resources", {}).get("requests", {}).get("memory", "0"))
                        for c in containers
                    ),
                }
            )

        return {"namespace": ns, "deployments": results}

    async def _handle_k8s_check_certificates(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp.get("namespace", "")
        cmd = ["get", "certificates", "-o", "json"]
        if ns:
            cmd.extend(["-n", ns])
        else:
            cmd.append("--all-namespaces")

        output = await self._kubectl(*cmd)
        if not output or "error" in output.lower():
            # Try secrets for TLS certs
            return {"certificates": [], "note": "cert-manager CRDs not found — check TLS secrets manually"}

        certs = json.loads(output).get("items", []) if output else []
        results = []
        for cert in certs:
            status = cert.get("status", {})
            conditions = {c["type"]: c for c in status.get("conditions", [])}
            results.append(
                {
                    "name": cert["metadata"]["name"],
                    "namespace": cert["metadata"].get("namespace", ""),
                    "ready": conditions.get("Ready", {}).get("status", "Unknown"),
                    "expiry": status.get("notAfter", "unknown"),
                    "renewal": status.get("renewalTime", ""),
                }
            )

        return {"certificates": results}

    async def _handle_k8s_get_hpa_status(self, inp: dict[str, Any]) -> dict[str, Any]:
        ns = inp["namespace"]
        output = await self._kubectl("get", "hpa", "-n", ns, "-o", "json")
        hpas = json.loads(output).get("items", []) if output else []

        results = []
        for h in hpas:
            spec = h.get("spec", {})
            status = h.get("status", {})
            results.append(
                {
                    "name": h["metadata"]["name"],
                    "target": spec.get("scaleTargetRef", {}).get("name", ""),
                    "min_replicas": spec.get("minReplicas", 1),
                    "max_replicas": spec.get("maxReplicas", 1),
                    "current_replicas": status.get("currentReplicas", 0),
                    "desired_replicas": status.get("desiredReplicas", 0),
                    "current_cpu_pct": status.get("currentCPUUtilizationPercentage"),
                }
            )

        return {"namespace": ns, "hpas": results}

    # --- Helpers ---

    async def _kubectl(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kubectl",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.debug("kubectl %s failed: %s", args[0], stderr.decode()[:200])
                return ""
            return stdout.decode(errors="replace")
        except (TimeoutError, FileNotFoundError) as e:
            logger.debug("kubectl unavailable: %s", e)
            return ""

    def _parse_cpu(self, cpu_str: str) -> float:
        if cpu_str.endswith("m"):
            return float(cpu_str[:-1]) / 1000
        try:
            return float(cpu_str)
        except ValueError:
            return 0

    def _parse_memory(self, mem_str: str) -> float:
        multipliers = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                return float(mem_str[: -len(suffix)]) * mult
        try:
            return float(mem_str)
        except ValueError:
            return 0
