"""Owner-filtered runtime evidence for registry agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from devai.agentic.kagent_client import RUNTIME_KAGENT, RUNTIME_LABEL
from devai.registry.client import Agent

logger = logging.getLogger(__name__)

_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_MANAGED_BY_VALUE = "agentic-registry"
_ORIGIN_AGENT_LABEL = "registry.agentic.dev/agent"
_MAX_CONTROLLER_AGENTS = 2_000
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_COLD_START_REASONS = frozenset({"ActorStarting", "ActorResuming", "ColdStarting"})


@dataclass(frozen=True, slots=True)
class ControllerAgent:
    origin: str
    name: str
    namespace: str
    kind: str
    model: str
    accepted: bool
    ready: bool
    reason: str
    ready_since: str
    worker_pool: str


@dataclass(frozen=True, slots=True)
class ControllerInventory:
    available: bool
    agents: tuple[ControllerAgent, ...]


def _condition(raw: Any, condition_type: str) -> dict[str, Any]:
    if not isinstance(raw, list):
        return {}
    for item in raw[:20]:
        if isinstance(item, dict) and str(item.get("type") or "") == condition_type:
            return item
    return {}


def controller_inventory_from_payload(payload: Any) -> ControllerInventory:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ControllerInventory(available=False, agents=())

    parsed: list[ControllerAgent] = []
    for item in payload["data"][:_MAX_CONTROLLER_AGENTS]:
        if not isinstance(item, dict):
            continue
        agent = item.get("agent")
        if not isinstance(agent, dict):
            continue
        metadata = agent.get("metadata")
        if not isinstance(metadata, dict):
            continue
        labels = metadata.get("labels")
        if not isinstance(labels, dict) or labels.get(_MANAGED_BY_LABEL) != _MANAGED_BY_VALUE:
            continue
        origin = str(labels.get(_ORIGIN_AGENT_LABEL) or "").strip()
        name = str(metadata.get("name") or "").strip()
        namespace = str(metadata.get("namespace") or "").strip()
        if not origin or not name or not namespace:
            continue

        status = agent.get("status") if isinstance(agent.get("status"), dict) else {}
        conditions = status.get("conditions") if isinstance(status, dict) else []
        accepted_condition = _condition(conditions, "Accepted")
        ready_condition = _condition(conditions, "Ready")
        accepted = bool(item.get("accepted")) or accepted_condition.get("status") == "True"
        ready = bool(item.get("deploymentReady")) or ready_condition.get("status") == "True"
        reason = str(ready_condition.get("reason") or ("Ready" if ready else "NotReady"))[:100]
        ready_since = str(ready_condition.get("lastTransitionTime") or "")[:64]

        raw_spec = agent.get("spec")
        spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
        raw_substrate = spec.get("substrate")
        substrate: dict[str, Any] = raw_substrate if isinstance(raw_substrate, dict) else {}
        pool_ref = substrate.get("workerPoolRef")
        worker_pool = str(pool_ref.get("name") or "")[:253] if isinstance(pool_ref, dict) else ""
        parsed.append(
            ControllerAgent(
                origin=origin,
                name=name,
                namespace=namespace,
                kind=str(agent.get("kind") or "")[:64],
                model=str(item.get("model") or "")[:200],
                accepted=accepted,
                ready=ready,
                reason=reason,
                ready_since=ready_since,
                worker_pool=worker_pool,
            )
        )
    return ControllerInventory(available=True, agents=tuple(parsed))


async def fetch_controller_inventory(base_url: str) -> ControllerInventory:
    if not base_url:
        return ControllerInventory(available=False, agents=())
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/agents")
            response.raise_for_status()
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise ValueError("controller response exceeds the runtime-status limit")
            return controller_inventory_from_payload(response.json())
    except Exception:  # noqa: BLE001 - runtime status must preserve the Job fallback
        logger.warning("kagent runtime inventory unavailable", exc_info=True)
        return ControllerInventory(available=False, agents=())


def _variant_status(item: ControllerAgent) -> dict[str, Any]:
    return {
        "name": item.name,
        "kind": item.kind,
        "model": item.model,
        "state": "ready" if item.ready else "unavailable",
        "reason": item.reason,
    }


def _job_status(reason: str) -> dict[str, Any]:
    return {
        "target": "job",
        "state": "on_demand",
        "runnable": True,
        "substrate_runnable": False,
        "reason": reason,
        "actor_state": "not_applicable",
        "ready_since": None,
        "last_run_at": None,
        "cold_start_ms": None,
        "run_latency_ms": None,
        "variants": [],
    }


def _substrate_status(items: list[ControllerAgent]) -> dict[str, Any]:
    sandbox_items = [item for item in items if item.kind == "SandboxAgent"]
    ready = next((item for item in sandbox_items if item.accepted and item.ready), None)
    cold_starting = next(
        (item for item in sandbox_items if item.accepted and item.reason in _COLD_START_REASONS),
        None,
    )
    if ready is not None:
        state = "ready"
        reason = ready.reason
        actor_state = "idle_or_scaled_to_zero"
        ready_since: str | None = ready.ready_since or None
        substrate_runnable = True
    elif cold_starting is not None:
        state = "cold_starting"
        reason = cold_starting.reason
        actor_state = "cold_starting"
        ready_since = None
        substrate_runnable = False
    elif not sandbox_items:
        state = "unavailable"
        reason = "sandbox_agent_not_reconciled"
        actor_state = "not_observed"
        ready_since = None
        substrate_runnable = False
    else:
        state = "unavailable"
        reason = sandbox_items[0].reason
        actor_state = "not_observed"
        ready_since = None
        substrate_runnable = False
    return {
        "target": "substrate",
        "state": state,
        "runnable": True,
        "substrate_runnable": substrate_runnable,
        "reason": reason,
        "actor_state": actor_state,
        "ready_since": ready_since,
        "last_run_at": None,
        "cold_start_ms": None,
        "run_latency_ms": None,
        "variants": [_variant_status(item) for item in sorted(items, key=lambda item: item.name)],
    }


def build_agent_runtime_snapshot(
    agents: list[Agent],
    inventory: ControllerInventory,
    *,
    substrate_enabled: bool,
    namespace: str,
) -> dict[str, Any]:
    by_origin: dict[str, list[ControllerAgent]] = {}
    for item in inventory.agents:
        if item.namespace == namespace:
            by_origin.setdefault(item.origin, []).append(item)

    statuses: dict[str, dict[str, Any]] = {}
    pools: set[str] = set()
    for agent in agents:
        runtime = str(agent.labels.get(RUNTIME_LABEL, "")).strip().lower()
        if runtime != RUNTIME_KAGENT:
            statuses[agent.name] = _job_status("job_default")
            continue
        if not substrate_enabled:
            statuses[agent.name] = _job_status("substrate_disabled")
            continue
        if not inventory.available:
            statuses[agent.name] = _job_status("controller_unavailable")
            continue
        runtime_items = by_origin.get(agent.name, [])
        if not runtime_items:
            status = _substrate_status([])
            status["state"] = "provisioning"
            status["reason"] = "awaiting_reconciliation"
            statuses[agent.name] = status
            continue
        statuses[agent.name] = _substrate_status(runtime_items)
        pools.update(item.worker_pool for item in runtime_items if item.worker_pool)

    return {
        "available": inventory.available,
        "substrate_enabled": substrate_enabled,
        "agents": statuses,
        "worker_pools": [
            {
                "name": name,
                "capacity": None,
                "occupancy": None,
                "headroom": None,
                "telemetry_available": False,
            }
            for name in sorted(pools)
        ],
        "latency": {
            "telemetry_available": False,
            "cold_start_ms": None,
            "run_latency_ms": None,
        },
    }


__all__ = [
    "ControllerInventory",
    "build_agent_runtime_snapshot",
    "controller_inventory_from_payload",
    "fetch_controller_inventory",
]
