"""Discovery Agent — autonomously discovers and maps the entire infrastructure.

This agent makes the SRE system fully autonomous by:
1. Auto-discovering all namespaces, deployments, services on the cluster
2. Mapping service-to-service dependencies (from network policies, Istio config)
3. Identifying databases, caches, message queues
4. Learning the topology without any manual configuration
5. Storing everything as semantic memory for other agents

Runs as the FIRST agent in every SRE scan — other agents consume its output.
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


class DiscoveryAgent:
    """Autonomously discovers cluster topology and app dependencies."""

    name = "discovery"

    def __init__(self, config: Any, memory: Any = None) -> None:
        self.config = config
        self.openai = OpenAIProvider(config)
        self.k8s = K8sToolExecutor()
        self.memory = memory

    async def run(self) -> dict[str, Any]:
        """Discover all services, databases, and dependencies on the cluster."""

        # 1. Discover all namespaces
        ns_raw = await self.k8s._kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}")
        all_namespaces = ns_raw.split() if ns_raw else []

        # Filter out system namespaces
        system_ns = {
            "kube-system",
            "kube-public",
            "kube-node-lease",
            "cert-manager",
            "istio-system",
            "argocd",
            "monitoring",
            "nats",
            "external-secrets",
        }
        app_namespaces = [ns for ns in all_namespaces if ns not in system_ns]
        infra_namespaces = [ns for ns in all_namespaces if ns in system_ns]

        # 2. Discover all deployments/services per namespace
        apps: list[dict[str, Any]] = []
        for ns in app_namespaces:
            deps_raw = await self.k8s._kubectl("get", "deployments,statefulsets,daemonsets", "-n", ns, "-o", "json")
            if not deps_raw:
                continue

            try:
                items = json.loads(deps_raw).get("items", [])
            except json.JSONDecodeError:
                continue

            for item in items:
                meta = item.get("metadata", {})
                spec = item.get("spec", {})
                containers = spec.get("template", {}).get("spec", {}).get("containers", [])

                # Extract environment variables to find database/cache connections
                env_vars = {}
                for c in containers:
                    for env in c.get("env", []):
                        name = env.get("name", "")
                        value = env.get("value", "")
                        if any(
                            kw in name.upper()
                            for kw in ["DATABASE", "DB_", "REDIS", "NATS", "KAFKA", "POSTGRES", "MONGO"]
                        ):
                            env_vars[name] = value or "(from secret)"

                apps.append(
                    {
                        "name": meta.get("name", ""),
                        "namespace": ns,
                        "kind": item.get("kind", "Deployment"),
                        "replicas": spec.get("replicas", 0),
                        "images": [c.get("image", "") for c in containers],
                        "ports": [p.get("containerPort") for c in containers for p in c.get("ports", [])],
                        "infra_connections": env_vars,
                    }
                )

        # 3. Discover services (to map endpoints)
        services: list[dict[str, Any]] = []
        for ns in app_namespaces:
            svc_raw = await self.k8s._kubectl("get", "services", "-n", ns, "-o", "json")
            if svc_raw:
                try:
                    for svc in json.loads(svc_raw).get("items", []):
                        svc_meta = svc.get("metadata", {})
                        svc_spec = svc.get("spec", {})
                        services.append(
                            {
                                "name": svc_meta.get("name", ""),
                                "namespace": ns,
                                "type": svc_spec.get("type", "ClusterIP"),
                                "ports": [
                                    {"port": p.get("port"), "target": p.get("targetPort")}
                                    for p in svc_spec.get("ports", [])
                                ],
                                "selector": svc_spec.get("selector", {}),
                            }
                        )
                except json.JSONDecodeError:
                    pass

        # 4. Discover Istio VirtualServices (if present)
        vs_raw = await self.k8s._kubectl("get", "virtualservices", "--all-namespaces", "-o", "json")
        virtual_services: list[dict[str, Any]] = []
        if vs_raw:
            try:
                for vs in json.loads(vs_raw).get("items", []):
                    vs_spec = vs.get("spec", {})
                    hosts = vs_spec.get("hosts", [])
                    virtual_services.append(
                        {
                            "name": vs["metadata"]["name"],
                            "namespace": vs["metadata"].get("namespace", ""),
                            "hosts": hosts,
                            "gateways": vs_spec.get("gateways", []),
                        }
                    )
            except json.JSONDecodeError:
                pass

        # 5. Use Groq to analyze topology and identify dependencies
        topology_input = json.dumps(
            {
                "app_namespaces": app_namespaces,
                "infra_namespaces": infra_namespaces,
                "apps": apps[:30],
                "services": services[:30],
                "virtual_services": virtual_services[:15],
            },
            indent=2,
        )

        topology_analysis = await self.openai.generate(
            prompt=f"""Analyze this Kubernetes cluster topology and map all dependencies:

{topology_input[:10000]}

Identify:
1. Which apps depend on which databases/caches
2. Service-to-service dependencies
3. External-facing services (via VirtualServices)
4. Potential single points of failure
5. Services without health checks""",
            system="You are a Kubernetes topology expert. Analyze the cluster and map all service dependencies. Output as JSON with 'dependency_graph', 'external_services', 'databases', 'single_points_of_failure' keys.",
            response_format={"type": "json_object"},
        )

        # 6. Store in memory for other agents
        if self.memory:
            try:
                await self.memory.learn_repo_pattern(
                    repo="cluster",
                    pattern_type="topology",
                    description=f"Cluster topology: {len(apps)} apps, {len(app_namespaces)} namespaces. {topology_analysis[:500]}",
                )
                # Store per-namespace info
                for ns in app_namespaces:
                    ns_apps = [a for a in apps if a["namespace"] == ns]
                    if ns_apps:
                        await self.memory.learn_repo_pattern(
                            repo=f"namespace:{ns}",
                            pattern_type="services",
                            description=f"Namespace {ns}: {', '.join(a['name'] for a in ns_apps)}",
                        )
            except Exception as e:
                logger.debug("Memory storage failed: %s", e)

        try:
            topology = json.loads(topology_analysis)
        except json.JSONDecodeError:
            topology = {}

        return {
            "namespaces": app_namespaces,
            "apps": apps,
            "services": services,
            "virtual_services": virtual_services,
            "topology": topology,
        }
