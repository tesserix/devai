"""Build K8s manifests (Job, Deployment, Service, VirtualService) for runners.

Every runner shares the same shape:

  * `image` — the devai-runner base, OR a stack-specific variant
    (`devai-runner-nextjs`, `devai-runner-go`, …) selected from
    `RuntimeConfig.runner_image_per_stack`.
  * `env` — task identity + registry endpoint + LLM secrets (mounted from
    `devai-api-secrets`). The runner reads these at startup, fetches its
    agent metadata from aregistry, downloads skills, then invokes the
    agent.
  * `volume` — emptyDir at `/devai/work` for the working tree; bind-mount
    a ConfigMap with the task slice at `/devai/task.json`.

Two output shapes:

  * `build_job_spec(...)` for run-to-completion stages (scaffold, install,
    test). Pod terminates when done.
  * `build_preview_manifests(...)` for long-lived preview pods. Returns a
    `Deployment + Service + Istio VirtualService` triple so the dev
    server is reachable at `https://preview-<run_id>.devai.tesserix.app`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.runtime.k8s_client import RuntimeConfig


# K8s name length limit. We squish task IDs into 26 chars max so the
# resulting Job name (`devai-runner-<task>-<stage>`) stays under 63.
_MAX_TASK_FRAG_LEN = 26
_DNS_SAFE = re.compile(r"[^a-z0-9-]+")


def _dns_safe(value: str, *, max_len: int = 63) -> str:
    """Coerce an arbitrary string into a DNS-1123 label.

    K8s names must match `[a-z0-9]([-a-z0-9]*[a-z0-9])?`. We lowercase,
    replace forbidden chars with `-`, strip leading/trailing dashes, and
    truncate.
    """
    s = _DNS_SAFE.sub("-", value.lower()).strip("-")
    if not s:
        s = "x"
    return s[:max_len]


@dataclass(slots=True)
class RunnerJobInputs:
    """Everything ``build_job_spec`` needs to render a Job.

    Kept as a dataclass so callers can construct it from any source
    (PipelineService, REST endpoint, CLI) without remembering positional
    order.

    The ``agent_profile`` field is the structured aregistry record for
    the agent that's about to run — ``{image, skills, prompts,
    mcp_servers, model_provider, model_name}``. It's serialized as JSON
    into ``DEVAI_AGENT_PROFILE`` so the runner pod gets the canonical
    answer without re-querying aregistry on boot (and so the dispatch
    decision is auditable in the Job's env block). None when aregistry
    is disabled or the agent isn't catalogued — the runner falls back to
    local YAML in that case.
    """

    task_id: str
    stage_name: str  # blueprint stage name (e.g. "scaffold_app")
    agent_name: str  # specialization key (e.g. "senior_developer")
    image: str  # full image:tag, overrides RuntimeConfig.runner_image
    repo: str  # owner/name
    intent: str  # user prompt
    blueprint: str  # which blueprint is driving the run
    extra_env: dict[str, str]  # stage.config carried straight through
    # Caller identity — flows from the boundary all the way into the Job.
    triggered_by: str = ""
    trace_id: str = ""
    # aregistry profile — see class docstring.
    agent_profile: dict[str, Any] | None = None
    # Agent-control-plane URLs — runner reads these to route MCP traffic
    # via agentgateway (if set) and to call kagent for A2A handoffs.
    agentgateway_url: str = ""
    kagent_url: str = ""


def build_job_spec(
    cfg: RuntimeConfig,
    inputs: RunnerJobInputs,
) -> dict[str, Any]:
    """Render a V1Job dict ready for `BatchV1Api.create_namespaced_job`.

    Returning a plain dict (vs `kubernetes_asyncio.client.V1Job`) keeps
    this file import-safe even when the SDK isn't installed (e.g. in
    unit tests). The K8sJobRuntime passes the dict through; the SDK
    accepts dicts for create requests.
    """
    task_frag = _dns_safe(inputs.task_id, max_len=_MAX_TASK_FRAG_LEN)
    stage_frag = _dns_safe(inputs.stage_name, max_len=16)
    job_name = _dns_safe(f"devai-runner-{task_frag}-{stage_frag}")

    labels = {
        "app.kubernetes.io/managed-by": "devai",
        "devai.tesserix.app/task-id": task_frag,
        "devai.tesserix.app/stage": stage_frag,
        "devai.tesserix.app/agent": _dns_safe(inputs.agent_name, max_len=32),
        "devai.tesserix.app/role": "runner",
    }

    env: list[dict[str, Any]] = [
        {"name": "DEVAI_RUNNER_AGENT", "value": inputs.agent_name},
        {"name": "DEVAI_RUNNER_TASK_ID", "value": inputs.task_id},
        {"name": "DEVAI_RUNNER_STAGE", "value": inputs.stage_name},
        {"name": "DEVAI_RUNNER_BLUEPRINT", "value": inputs.blueprint},
        {"name": "DEVAI_RUNNER_REPO", "value": inputs.repo},
        {"name": "DEVAI_RUNNER_INTENT", "value": inputs.intent},
        {"name": "DEVAI_REGISTRY_URL", "value": cfg.registry_url},
        # Stage handlers pass their YAML `config:` block as a JSON blob so
        # the runner sees the exact values without re-parsing YAML.
        {"name": "DEVAI_STAGE_CONFIG", "value": json.dumps(inputs.extra_env or {})},
        # Caller identity — propagates the originating user end-to-end.
        # The runner stamps these onto its A2A messages and structured logs.
        {"name": "DEVAI_TRIGGERED_BY", "value": inputs.triggered_by or ""},
        {"name": "DEVAI_TRACE_ID", "value": inputs.trace_id or ""},
        # Canonical aregistry record for this agent. Pre-resolved by the
        # dispatcher so the runner doesn't have to round-trip aregistry on
        # boot (and so the env block is the source of truth for what was
        # actually dispatched). JSON or empty string.
        {
            "name": "DEVAI_AGENT_PROFILE",
            "value": json.dumps(inputs.agent_profile) if inputs.agent_profile else "",
        },
        # Agent control-plane URLs. Empty means "no gateway, talk direct".
        {"name": "DEVAI_AGENTGATEWAY_URL", "value": inputs.agentgateway_url or ""},
        {"name": "DEVAI_KAGENT_URL", "value": inputs.kagent_url or ""},
    ]
    # Secrets reused from devai-api-secrets so the runner can talk to the
    # same LLM gateways without holding a separate copy.
    for secret_key in (
        "DEVAI_ANTHROPIC_API_KEY",
        "DEVAI_OPENAI_API_KEY",
        "DEVAI_GROQ_API_KEY",
        "DEVAI_GEMINI_API_KEY",
        "DEVAI_SCM_TOKEN",
    ):
        env.append(
            {
                "name": secret_key,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "devai-api-secrets",
                        "key": secret_key,
                        "optional": True,
                    }
                },
            }
        )

    container: dict[str, Any] = {
        "name": "runner",
        "image": inputs.image or cfg.runner_image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "devai.runner.entrypoint"],
        "env": env,
        "volumeMounts": [
            {"name": "workspace", "mountPath": "/devai/work"},
            {"name": "task-slice", "mountPath": "/devai/task", "readOnly": True},
        ],
        "resources": {
            "requests": {"cpu": "200m", "memory": "512Mi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
    }

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "serviceAccountName": cfg.service_account_name,
        "securityContext": cfg.pod_security_context,
        "containers": [container],
        "volumes": [
            {"name": "workspace", "emptyDir": {"sizeLimit": "8Gi"}},
            {
                "name": "task-slice",
                "configMap": {"name": f"{job_name}-task", "optional": True},
            },
        ],
    }
    if cfg.pull_secret_name:
        pod_spec["imagePullSecrets"] = [{"name": cfg.pull_secret_name}]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": cfg.namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": cfg.default_backoff_limit,
            "ttlSecondsAfterFinished": cfg.default_ttl_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": pod_spec,
            },
        },
    }


@dataclass(slots=True)
class PreviewInputs:
    """Inputs for the long-lived preview pod.

    The preview pod runs the dev server (Next.js, Vite, Astro, etc.) and
    a `claude-code-bridge` sidecar that exposes a WebSocket the dashboard
    editor panel connects to. The git working tree is shared via an
    emptyDir mounted by both containers — file edits from the editor are
    instantly visible to the dev server's HMR loop.
    """

    run_id: str
    repo: str
    image: str  # stack-specific dev-server image
    branch: str  # the scaffold branch
    dev_port: int  # 3000 for Next/React, 5173 for Vite, …
    dev_command: list[str]  # ["npm", "run", "dev"]
    editor_bridge_image: str  # devai-claude-code-bridge:main
    editor_bridge_port: int = 7681


def build_preview_manifests(
    cfg: RuntimeConfig,
    inputs: PreviewInputs,
) -> dict[str, dict[str, Any]]:
    """Return `{deployment, service, virtualservice}` for the preview pod.

    The Deployment runs two containers in one pod:

      * `dev-server` — the stack-specific image, runs `npm run dev`.
      * `editor-bridge` — claude-code-bridge, exposes ttyd/WebSocket on
        port 7681. Mounts the same workspace volume so its edits show up
        instantly in the dev server's HMR.

    The Service fronts the dev-server port. The Istio VirtualService
    routes `preview-<run_id>.devai.tesserix.app` → the Service, and
    `editor-<run_id>.devai.tesserix.app` → the bridge port.
    """
    name_frag = _dns_safe(inputs.run_id, max_len=24)
    name = f"devai-preview-{name_frag}"
    workspace = "workspace"

    labels = {
        "app.kubernetes.io/managed-by": "devai",
        "app.kubernetes.io/name": name,
        "devai.tesserix.app/run-id": name_frag,
        "devai.tesserix.app/role": "preview",
        "devai.tesserix.app/repo": _dns_safe(inputs.repo, max_len=48),
    }

    pod_volumes = [
        {"name": workspace, "emptyDir": {"sizeLimit": "8Gi"}},
    ]

    init_containers = [
        {
            "name": "git-clone",
            "image": "alpine/git:2.43.0",
            "command": [
                "sh",
                "-lc",
                (
                    "git clone --branch ${BRANCH} --depth 1 "
                    "https://x-access-token:${DEVAI_SCM_TOKEN}@github.com/${REPO}.git /work && "
                    "chown -R 1000:1000 /work"
                ),
            ],
            "env": [
                {"name": "REPO", "value": inputs.repo},
                {"name": "BRANCH", "value": inputs.branch},
                {
                    "name": "DEVAI_SCM_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "devai-github-pat",
                            "key": "token",
                            "optional": True,
                        }
                    },
                },
            ],
            "volumeMounts": [{"name": workspace, "mountPath": "/work"}],
        }
    ]

    dev_container = {
        "name": "dev-server",
        "image": inputs.image,
        "imagePullPolicy": "IfNotPresent",
        "workingDir": "/work",
        "command": inputs.dev_command,
        "env": [
            {"name": "HOST", "value": "0.0.0.0"},
            {"name": "PORT", "value": str(inputs.dev_port)},
            {"name": "BROWSER", "value": "none"},
            {"name": "NODE_ENV", "value": "development"},
        ],
        "ports": [{"name": "dev", "containerPort": inputs.dev_port}],
        "volumeMounts": [{"name": workspace, "mountPath": "/work"}],
        "readinessProbe": {
            "tcpSocket": {"port": inputs.dev_port},
            "initialDelaySeconds": 5,
            "periodSeconds": 5,
        },
        "resources": {
            "requests": {"cpu": "250m", "memory": "768Mi"},
            "limits": {"cpu": "2", "memory": "3Gi"},
        },
    }

    bridge_container = {
        "name": "editor-bridge",
        "image": inputs.editor_bridge_image,
        "imagePullPolicy": "IfNotPresent",
        "workingDir": "/work",
        # The bridge runs `claude` in agent mode and exposes a WebSocket
        # the dashboard's editor pane talks to. ttyd is a common
        # transport but the image can use any websocket → pty wiring.
        "command": ["/usr/local/bin/start-bridge.sh"],
        "env": [
            {"name": "EDITOR_PORT", "value": str(inputs.editor_bridge_port)},
            {
                "name": "DEVAI_ANTHROPIC_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "devai-api-secrets",
                        "key": "DEVAI_ANTHROPIC_API_KEY",
                        "optional": True,
                    }
                },
            },
        ],
        "ports": [{"name": "editor", "containerPort": inputs.editor_bridge_port}],
        "volumeMounts": [{"name": workspace, "mountPath": "/work"}],
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": cfg.namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": cfg.service_account_name,
                    "securityContext": cfg.pod_security_context,
                    "initContainers": init_containers,
                    "containers": [dev_container, bridge_container],
                    "volumes": pod_volumes,
                    **({"imagePullSecrets": [{"name": cfg.pull_secret_name}]} if cfg.pull_secret_name else {}),
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": cfg.namespace, "labels": labels},
        "spec": {
            "selector": {"app.kubernetes.io/name": name},
            "ports": [
                {"name": "dev", "port": 80, "targetPort": inputs.dev_port},
                {
                    "name": "editor",
                    "port": 81,
                    "targetPort": inputs.editor_bridge_port,
                },
            ],
        },
    }

    preview_host = f"preview-{name_frag}.{cfg.preview_domain}"
    editor_host = f"editor-{name_frag}.{cfg.preview_domain}"
    virtualservice = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "VirtualService",
        "metadata": {"name": name, "namespace": cfg.namespace, "labels": labels},
        "spec": {
            "hosts": [preview_host, editor_host],
            "gateways": ["istio-ingress/devai-gateway"],
            "http": [
                {
                    "match": [{"authority": {"exact": preview_host}}],
                    "route": [
                        {
                            "destination": {
                                "host": f"{name}.{cfg.namespace}.svc.cluster.local",
                                "port": {"number": 80},
                            }
                        }
                    ],
                },
                {
                    "match": [{"authority": {"exact": editor_host}}],
                    "route": [
                        {
                            "destination": {
                                "host": f"{name}.{cfg.namespace}.svc.cluster.local",
                                "port": {"number": 81},
                            }
                        }
                    ],
                },
            ],
        },
    }

    return {
        "deployment": deployment,
        "service": service,
        "virtualservice": virtualservice,
        "preview_host": preview_host,  # type: ignore[dict-item]
        "editor_host": editor_host,  # type: ignore[dict-item]
    }


__all__ = [
    "PreviewInputs",
    "RunnerJobInputs",
    "build_job_spec",
    "build_preview_manifests",
]
