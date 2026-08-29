"""Cluster projection and endpoint derivation for imported portable agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from devai.sandbox.egress import proxy_env
from devai.sandbox.job import SANDBOX_LABEL

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord


def portable_runtime_name(sandbox_id: str) -> str:
    return f"devai-agent-{sandbox_id}"


def portable_runtime_endpoint(record: SandboxRecord, *, namespace: str) -> str:
    runtime = _runtime(record)
    if runtime["type"] == "remote":
        return _remote_url(runtime)
    port = _port(runtime)
    path = _path(runtime)
    name = portable_runtime_name(record.id)
    return f"http://{name}.{namespace}.svc.cluster.local:{port}{path}"


def build_portable_runtime_manifests(record: SandboxRecord, *, namespace: str) -> list[dict[str, Any]]:
    runtime = _runtime(record)
    if runtime["type"] == "remote":
        _remote_url(runtime)
        return []
    if runtime["type"] != "container":
        raise ValueError("portable runtime type must be container or remote")
    image = str(runtime.get("image") or "")
    if "@sha256:" not in image:
        raise ValueError("portable container image is not digest-pinned")
    port = _port(runtime)
    path = _path(runtime)
    health_path = _safe_path(runtime.get("healthPath") or "/healthz", field="healthPath")
    name = portable_runtime_name(record.id)
    labels = {
        "app.kubernetes.io/name": "devai-portable-agent",
        "app.kubernetes.io/managed-by": "devai",
        SANDBOX_LABEL: record.id,
    }
    metadata = {"name": name, "namespace": namespace, "labels": labels}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "devai-sandbox",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "agent",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "agent", "containerPort": port}],
                            "env": [
                                {"name": "DEVAI_SANDBOX_ID", "value": record.id},
                                {"name": "PORT", "value": str(port)},
                                *proxy_env(record.id, namespace=namespace),
                            ],
                            "readinessProbe": {"httpGet": {"path": health_path, "port": "agent"}},
                            "livenessProbe": {"tcpSocket": {"port": "agent"}},
                            "resources": {
                                "requests": {"cpu": "200m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "4Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "selector": labels,
            "ports": [{"name": "agent", "port": port, "targetPort": port}],
        },
    }
    _safe_path(path, field="path")
    return [deployment, service]


def _runtime(record: SandboxRecord) -> dict[str, Any]:
    snapshot = record.spec.import_snapshot
    if snapshot is None:
        raise ValueError("sandbox does not carry an imported runtime snapshot")
    return snapshot.runtime


def _port(runtime: dict[str, Any]) -> int:
    value = runtime.get("port", 8080)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("portable runtime port must be between 1 and 65535")
    return value


def _path(runtime: dict[str, Any]) -> str:
    default = "/a2a/v1" if runtime.get("protocol") == "a2a" else "/invoke"
    return _safe_path(runtime.get("path") or default, field="path")


def _safe_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or ".." in value.split("/"):
        raise ValueError(f"portable runtime {field} must be an absolute path without traversal")
    return value


def _remote_url(runtime: dict[str, Any]) -> str:
    url = runtime.get("url")
    if not isinstance(url, str):
        raise ValueError("portable remote runtime must use an absolute HTTPS URL without user info")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("portable remote runtime must use an absolute HTTPS URL without user info")
    return url


__all__ = [
    "build_portable_runtime_manifests",
    "portable_runtime_endpoint",
    "portable_runtime_name",
]
