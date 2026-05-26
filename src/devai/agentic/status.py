"""Snapshot of the agentic control plane for the dashboard.

``fetch_agentic_status(...)`` probes each in-cluster service and
returns a single ``AgenticStatus`` object. Designed to be fast and
fail-soft: any one probe failing leaves the others intact and is
reported as ``reachable=False`` on the affected component, rather
than 5xx-ing the whole call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from devai.registry import RegistryClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ComponentStatus:
    """Live state of one control-plane component."""

    name: str
    role: str            # "registry" | "gateway" | "controller" | "llm-proxy"
    namespace: str
    url: str
    reachable: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class AgenticStatus:
    """Aggregated snapshot for the dashboard."""

    registry: ComponentStatus
    agentgateway: ComponentStatus
    ai_gateway: ComponentStatus
    kagent: ComponentStatus
    catalog: dict[str, int] = field(default_factory=dict)


def fetch_agentic_status(
    *,
    registry_client: RegistryClient | None,
    agentgateway_url: str = "",
    ai_gateway_url: str = "",
    kagent_url: str = "",
) -> AgenticStatus:
    """Build the snapshot. Each probe is best-effort + bounded.

    ``registry_client`` is optional — when ``None`` the registry block
    reports unreachable but the call still succeeds.
    """

    # --- agentregistry (catalog) ----
    registry = ComponentStatus(
        name="Agent Registry",
        role="registry",
        namespace="agentregistry-system",
        url=registry_client._base_url if registry_client else "",  # type: ignore[attr-defined]
        reachable=False,
    )
    catalog: dict[str, int] = {}
    if registry_client is not None:
        try:
            health = registry_client.health()
            registry.reachable = bool(health.get("reachable"))
            registry.detail = {k: v for k, v in health.items() if k != "reachable"}
            if registry.reachable:
                catalog = registry_client.counts()
        except Exception as e:  # noqa: BLE001
            registry.error = str(e)
    else:
        registry.error = "registry client not configured"

    # --- agentgateway (Solo.io control plane) ----
    # The xDS port (9978) is gRPC — HEAD doesn't speak to it. We probe
    # the metrics endpoint (9092) which is always served by the
    # controller when it's healthy. A 200 there is the simplest live-
    # ness signal for the chart.
    agentgateway = _probe_http(
        name="Agent Gateway (Solo.io)",
        role="controller",
        namespace="agentgateway-system",
        url=agentgateway_url or "http://agentgateway.agentgateway-system.svc.cluster.local:9092",
        path="/metrics",
        success_code_range=(200, 300),
    )
    # Most-useful detail: whether we have any AgentgatewayBackend or
    # HTTPRoute resources for it. We can't list those without K8s API
    # access; the dashboard's Gateway panel surfaces the controller
    # health + the LLM data-plane (ai-gateway) status side-by-side.

    # --- ai-gateway (LLM forwarder) ----
    ai_gateway = _probe_http(
        name="AI Gateway",
        role="llm-proxy",
        namespace="agentgateway-system",
        url=ai_gateway_url or "http://ai-gateway.agentgateway-system.svc.cluster.local:8080",
        path="/health",
        success_code_range=(200, 300),
        success_body_includes="ok",
    )

    # --- kagent (agent runtime controller) ----
    # /healthz returns 404 on kagent 0.9.x — the binary exposes /version
    # instead, which serves a small JSON ({kagent_version, git_commit,
    # build_date}). It's cheap, always-on, and a reliable signal that
    # the controller process is up and serving its HTTP API.
    kagent = _probe_http(
        name="kagent",
        role="controller",
        namespace="kagent-system",
        url=kagent_url or "http://kagent-controller.kagent-system.svc.cluster.local:8083",
        path="/version",
        success_code_range=(200, 300),
    )

    return AgenticStatus(
        registry=registry,
        agentgateway=agentgateway,
        ai_gateway=ai_gateway,
        kagent=kagent,
        catalog=catalog,
    )


def to_dict(status: AgenticStatus) -> dict[str, Any]:
    """Serialise — strips private fields, keeps the wire shape stable."""
    return asdict(status)


def _probe_http(
    *,
    name: str,
    role: str,
    namespace: str,
    url: str,
    path: str,
    success_code_range: tuple[int, int] = (200, 300),
    success_body_includes: str = "",
    timeout_seconds: float = 3.0,
) -> ComponentStatus:
    """Single tiny HTTP probe. Returns a ComponentStatus.

    httpx is lazy-imported to keep the cold-start light when the
    Gateway page isn't loaded.
    """
    cs = ComponentStatus(
        name=name, role=role, namespace=namespace, url=url, reachable=False
    )
    if os.environ.get("DEVAI_AGENTIC_PROBES", "true").lower() == "false":
        cs.error = "probes disabled (DEVAI_AGENTIC_PROBES=false)"
        return cs
    try:
        import httpx  # type: ignore[import-untyped]
    except ImportError as e:
        cs.error = f"httpx missing: {e}"
        return cs

    target = url.rstrip("/") + path
    try:
        r = httpx.get(target, timeout=timeout_seconds)
    except httpx.HTTPError as e:
        cs.error = f"{type(e).__name__}: {e}"
        return cs

    lo, hi = success_code_range
    if r.status_code < lo or r.status_code >= hi:
        cs.error = f"HTTP {r.status_code}"
        cs.detail["status_code"] = r.status_code
        return cs
    if success_body_includes and success_body_includes not in r.text:
        cs.error = f"unexpected body (HTTP {r.status_code})"
        cs.detail["status_code"] = r.status_code
        return cs

    cs.reachable = True
    cs.detail["status_code"] = r.status_code
    return cs
