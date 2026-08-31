"""Cluster-side boundary for a sandbox: quota, limits and egress.

Deliberately built on the *existing* namespace — a sandbox is a set of scoped
objects selecting one label, not a new cluster or node pool. Egress is
default-deny with an explicit allow-list, because an agent chooses its actions at
runtime: the interesting failure is the one nobody predicted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devai.sandbox.job import SANDBOX_LABEL

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

# Multiples of the per-pod ceiling in `build_job_spec`, so one runaway sandbox
# can hold at most a couple of runner-sized pods.
_QUOTA_PODS = "4"
_QUOTA_CPU = "8"
_QUOTA_MEMORY = "16Gi"

_MAX_CONTAINER_CPU = "2"
_MAX_CONTAINER_MEMORY = "4Gi"


def _meta(sandbox_id: str, prefix: str, namespace: str) -> dict[str, Any]:
    return {
        "name": f"{prefix}-{sandbox_id}",
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "devai",
            SANDBOX_LABEL: sandbox_id,
        },
    }


def _selector(sandbox_id: str) -> dict[str, Any]:
    return {"matchLabels": {SANDBOX_LABEL: sandbox_id}}


def build_isolation_manifests(
    record: SandboxRecord, *, namespace: str, control_plane_namespace: str = "devai"
) -> list[dict[str, Any]]:
    """Return the objects that fence this sandbox in, in creation order."""
    sid = record.id
    # The namespace itself is the fence; no PriorityClass scope split needed.
    quota = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": _meta(sid, "devai-sandbox-quota", namespace),
        "spec": {
            "hard": {
                "pods": _QUOTA_PODS,
                "requests.cpu": _QUOTA_CPU,
                "requests.memory": _QUOTA_MEMORY,
                "limits.cpu": _QUOTA_CPU,
                "limits.memory": _QUOTA_MEMORY,
            },
        },
    }
    limits = {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": _meta(sid, "devai-sandbox-limits", namespace),
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {"cpu": _MAX_CONTAINER_CPU, "memory": _MAX_CONTAINER_MEMORY},
                    "defaultRequest": {"cpu": "200m", "memory": "512Mi"},
                    "max": {"cpu": _MAX_CONTAINER_CPU, "memory": _MAX_CONTAINER_MEMORY},
                }
            ]
        },
    }
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _meta(sid, "devai-sandbox-egress", namespace),
        "spec": {
            "podSelector": _selector(sid),
            "policyTypes": ["Egress", "Ingress"],
            "egress": _egress_rules(namespace, control_plane_namespace),
            "ingress": _ingress_rules(control_plane_namespace),
        },
    }
    return [quota, limits, policy]


def _egress_rules(namespace: str, control_plane_namespace: str) -> list[dict[str, Any]]:
    """DNS, the sandbox's own namespace, then the control-plane *pods* only.

    Nothing else, including the internet — a sandboxed agent reaches the
    outside world through the tool gateway, which is the thing that can record
    and mock the call. The pod selector on the control-plane rule is what
    keeps Postgres/Redis in that namespace unreachable.
    """
    return [
        {
            "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {
            "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}}}],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": control_plane_namespace}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "devai"}},
                }
            ],
        },
    ]


def _ingress_rules(control_plane_namespace: str) -> list[dict[str, Any]]:
    """The control-plane pods, and anything in the sandbox's own namespace.

    Cross-sandbox traffic is impossible by namespace boundary now; the empty
    podSelector admits only this namespace's own pods (runner → workspace).
    """
    return [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": control_plane_namespace}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "devai"}},
                },
                {"podSelector": {}},
            ]
        }
    ]


__all__ = ["build_isolation_manifests"]
