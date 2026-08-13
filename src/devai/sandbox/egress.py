"""The one route out of a sandbox: a per-sandbox allowlisting forward proxy.

#180 fenced sandboxes off the internet entirely, which is safe and unusable —
real work needs pypi, npm and github. The temptation is to open 443, which
throws the control away. A proxy keeps all three properties at once: the agent
can install what it needs, every outbound request is attributable, and a blocked
request fails legibly instead of hanging.

One proxy per sandbox rather than one shared proxy, so a per-sandbox allowlist
addition is really per-sandbox and the access log needs no demultiplexing before
it can join that run's trace.

Limitation, stated rather than implied away: a domain allowlist cannot tell a
``git clone`` from a ``git push`` to an attacker's repo on an allowed domain.
Exfiltration through an allowed host is bounded by what credentials the sandbox
holds, not by this proxy.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from devai.sandbox.job import SANDBOX_LABEL

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

PROXY_PORT = 8118

# What an agent needs to build software, and nothing else. A leading dot means
# "this domain and its subdomains". Adding to this list is meant to take a PR:
# the friction is what stops an allowlist eroding into allow-all.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "pypi.org",
    ".pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    ".github.com",
    ".githubusercontent.com",
    "proxy.golang.org",
    "sum.golang.org",
    "ghcr.io",
    "api.anthropic.com",
    "api.openai.com",
    "api.groq.com",
)

_NO_PROXY = "localhost,127.0.0.1,::1,.svc,.svc.cluster.local,.cluster.local"


def host_allowed(host: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    """Match on domain boundaries — ``notgithub.com`` is not ``github.com``."""
    candidate = host.strip().lower().rsplit(":", 1)[0].rstrip(".")
    if not candidate:
        return False
    for pattern in allowlist:
        entry = pattern.strip().lower().lstrip(".")
        if not entry:
            continue
        if candidate == entry or candidate.endswith(f".{entry}"):
            return True
    return False


def resolve_allowlist(record: SandboxRecord) -> list[str]:
    return [*DEFAULT_ALLOWLIST, *record.spec.allow_domains]


def proxy_name(sandbox_id: str) -> str:
    return f"devai-sandbox-proxy-{sandbox_id}"


def proxy_service_host(sandbox_id: str, *, namespace: str) -> str:
    return f"{proxy_name(sandbox_id)}.{namespace}.svc.cluster.local:{PROXY_PORT}"


def proxy_env(sandbox_id: str, *, namespace: str | None = None) -> list[dict[str, str]]:
    """Both cases: curl reads the lower-case names, most runtimes the upper."""
    ns = namespace or os.environ.get("DEVAI_NAMESPACE") or "devai"
    url = f"http://{proxy_service_host(sandbox_id, namespace=ns)}"
    return [{"name": name, "value": url} for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")] + [
        {"name": "NO_PROXY", "value": _NO_PROXY},
        {"name": "no_proxy", "value": _NO_PROXY},
    ]


def _meta(sandbox_id: str, namespace: str) -> dict[str, Any]:
    return {
        "name": proxy_name(sandbox_id),
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "devai",
            "app.kubernetes.io/component": "sandbox-proxy",
            SANDBOX_LABEL: sandbox_id,
        },
    }


def build_proxy_manifests(record: SandboxRecord, *, namespace: str, image: str = "") -> list[dict[str, Any]]:
    """The egress proxy objects for one sandbox, in creation order."""
    sid = record.id
    meta = _meta(sid, namespace)
    name = meta["name"]

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": meta,
        "data": {"allowlist": "\n".join(resolve_allowlist(record))},
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta,
        "spec": {
            "restartPolicy": "Always",
            "serviceAccountName": "devai-sandbox",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "fsGroup": 1000},
            "containers": [
                {
                    "name": "proxy",
                    "image": image or _default_image(),
                    "command": ["python", "-m", "devai.sandbox.egress_proxy"],
                    "ports": [{"containerPort": PROXY_PORT, "name": "proxy"}],
                    "env": [
                        {"name": "DEVAI_SANDBOX_ID", "value": sid},
                        {"name": "DEVAI_EGRESS_ALLOWLIST_FILE", "value": "/etc/devai/allowlist"},
                    ],
                    "volumeMounts": [{"name": "allowlist", "mountPath": "/etc/devai"}],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "256Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [{"name": "allowlist", "configMap": {"name": name}}],
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": meta,
        "spec": {
            "type": "ClusterIP",
            "selector": {SANDBOX_LABEL: sid, "app.kubernetes.io/component": "sandbox-proxy"},
            "ports": [{"port": PROXY_PORT, "targetPort": PROXY_PORT, "name": "proxy"}],
        },
    }
    return [configmap, pod, service]


def _default_image() -> str:
    return os.getenv("DEVAI_RUNNER_IMAGE", "ghcr.io/tesserix/devai/devai-runner:latest")


__all__ = [
    "DEFAULT_ALLOWLIST",
    "PROXY_PORT",
    "build_proxy_manifests",
    "host_allowed",
    "proxy_env",
    "proxy_name",
    "proxy_service_host",
    "resolve_allowlist",
]
