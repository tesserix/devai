from __future__ import annotations

from devai.agentic.runtime_status import (
    ControllerInventory,
    build_agent_runtime_snapshot,
    controller_inventory_from_payload,
)
from devai.registry.client import Agent


def _agent(name: str, *, runtime: str = "") -> Agent:
    labels = {"devai.io/runtime": runtime} if runtime else {}
    return Agent(name=name, description="", version="1", labels=labels)


def test_ready_sandbox_agent_is_the_only_live_running_evidence() -> None:
    inventory = controller_inventory_from_payload(
        {
            "data": [
                {
                    "agent": {
                        "kind": "SandboxAgent",
                        "metadata": {
                            "name": "reviewer-openai",
                            "namespace": "kagent-system",
                            "labels": {
                                "app.kubernetes.io/managed-by": "agentic-registry",
                                "registry.agentic.dev/agent": "reviewer",
                            },
                        },
                        "spec": {"substrate": {"workerPoolRef": {"name": "kagent-default"}}},
                        "status": {
                            "conditions": [
                                {"type": "Accepted", "status": "True", "reason": "Reconciled"},
                                {
                                    "type": "Ready",
                                    "status": "True",
                                    "reason": "ActorTemplateReady",
                                    "lastTransitionTime": "2026-08-19T10:00:00Z",
                                },
                            ]
                        },
                    },
                    "model": "gpt-4.1",
                    "workloadMode": "sandbox",
                    "deploymentReady": True,
                    "accepted": True,
                }
            ]
        }
    )

    snapshot = build_agent_runtime_snapshot(
        [_agent("reviewer", runtime="kagent")],
        inventory,
        substrate_enabled=True,
        namespace="kagent-system",
    )

    assert snapshot["agents"]["reviewer"] == {
        "target": "substrate",
        "state": "ready",
        "runnable": True,
        "substrate_runnable": True,
        "reason": "ActorTemplateReady",
        "actor_state": "idle_or_scaled_to_zero",
        "ready_since": "2026-08-19T10:00:00Z",
        "last_run_at": None,
        "cold_start_ms": None,
        "run_latency_ms": None,
        "variants": [
            {
                "name": "reviewer-openai",
                "kind": "SandboxAgent",
                "model": "gpt-4.1",
                "state": "ready",
                "reason": "ActorTemplateReady",
            }
        ],
    }
    assert snapshot["worker_pools"] == [
        {
            "name": "kagent-default",
            "capacity": None,
            "occupancy": None,
            "headroom": None,
            "telemetry_available": False,
        }
    ]


def test_classic_agent_or_foreign_origin_cannot_forge_substrate_running() -> None:
    inventory = controller_inventory_from_payload(
        {
            "data": [
                {
                    "agent": {
                        "kind": "Agent",
                        "metadata": {
                            "name": "reviewer-openai",
                            "namespace": "kagent-system",
                            "labels": {
                                "app.kubernetes.io/managed-by": "agentic-registry",
                                "registry.agentic.dev/agent": "reviewer",
                            },
                        },
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    "deploymentReady": True,
                },
                {
                    "agent": {
                        "kind": "SandboxAgent",
                        "metadata": {
                            "name": "foreign-openai",
                            "namespace": "kagent-system",
                            "labels": {
                                "app.kubernetes.io/managed-by": "agentic-registry",
                                "registry.agentic.dev/agent": "foreign",
                            },
                        },
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    "deploymentReady": True,
                },
            ]
        }
    )

    status = build_agent_runtime_snapshot(
        [_agent("reviewer", runtime="kagent")],
        inventory,
        substrate_enabled=True,
        namespace="kagent-system",
    )["agents"]["reviewer"]

    assert status["state"] == "unavailable"
    assert status["substrate_runnable"] is False
    assert status["reason"] == "sandbox_agent_not_reconciled"


def test_dormant_and_unreachable_substrate_report_job_fallback_honestly() -> None:
    agent = _agent("reviewer", runtime="kagent")
    available = ControllerInventory(available=True, agents=())
    unavailable = ControllerInventory(available=False, agents=())

    dormant = build_agent_runtime_snapshot([agent], available, substrate_enabled=False, namespace="kagent-system")[
        "agents"
    ]["reviewer"]
    degraded = build_agent_runtime_snapshot([agent], unavailable, substrate_enabled=True, namespace="kagent-system")[
        "agents"
    ]["reviewer"]

    assert (dormant["target"], dormant["state"], dormant["reason"]) == (
        "job",
        "on_demand",
        "substrate_disabled",
    )
    assert (degraded["target"], degraded["state"], degraded["reason"]) == (
        "job",
        "on_demand",
        "controller_unavailable",
    )
    assert dormant["runnable"] is True and degraded["runnable"] is True
    assert dormant["substrate_runnable"] is False and degraded["substrate_runnable"] is False


def test_explicit_actor_start_condition_is_reported_as_cold_starting() -> None:
    inventory = controller_inventory_from_payload(
        {
            "data": [
                {
                    "agent": {
                        "kind": "SandboxAgent",
                        "metadata": {
                            "name": "reviewer",
                            "namespace": "kagent-system",
                            "labels": {
                                "app.kubernetes.io/managed-by": "agentic-registry",
                                "registry.agentic.dev/agent": "reviewer",
                            },
                        },
                        "status": {
                            "conditions": [
                                {"type": "Accepted", "status": "True"},
                                {"type": "Ready", "status": "False", "reason": "ActorResuming"},
                            ]
                        },
                    },
                    "accepted": True,
                }
            ]
        }
    )

    status = build_agent_runtime_snapshot(
        [_agent("reviewer", runtime="kagent")],
        inventory,
        substrate_enabled=True,
        namespace="kagent-system",
    )["agents"]["reviewer"]

    assert status["state"] == "cold_starting"
    assert status["actor_state"] == "cold_starting"
    assert status["substrate_runnable"] is False


def test_controller_messages_and_unmanaged_records_are_not_returned() -> None:
    inventory = controller_inventory_from_payload(
        {
            "data": [
                {
                    "agent": {
                        "kind": "SandboxAgent",
                        "metadata": {
                            "name": "reviewer",
                            "namespace": "kagent-system",
                            "labels": {"registry.agentic.dev/agent": "reviewer"},
                        },
                        "status": {
                            "conditions": [
                                {
                                    "type": "Ready",
                                    "status": "False",
                                    "reason": "ActorTemplateNotReady",
                                    "message": "internal address and credential detail",
                                }
                            ]
                        },
                    }
                }
            ]
        }
    )

    assert inventory.agents == ()
    snapshot = build_agent_runtime_snapshot(
        [_agent("reviewer")], inventory, substrate_enabled=True, namespace="kagent-system"
    )
    assert "internal address" not in str(snapshot)
    assert snapshot["agents"]["reviewer"]["state"] == "on_demand"
