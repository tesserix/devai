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
    # The Service name == the leftmost DNS label of the preview host, which is
    # the convention the devai-auth-bff preview proxy resolves on
    # (preview-<id>.tesserix.app → Service preview-<id> in the previews ns).
    name = f"preview-{name_frag}"
    ns = cfg.preview_namespace
    workspace = "workspace"
    pvc_name = f"{name}-work"

    labels = {
        "app.kubernetes.io/managed-by": "devai",
        "app.kubernetes.io/name": name,
        "devai.tesserix.app/run-id": name_frag,
        "devai.tesserix.app/role": "preview",
        "devai.tesserix.app/repo": _dns_safe(inputs.repo, max_len=48),
    }

    # A PVC (not emptyDir) so the workspace + node_modules survive pod
    # restarts and the editor-bridge edits persist — the basis for the
    # hot-edit loop and edit→git.
    pod_volumes = [
        {"name": workspace, "persistentVolumeClaim": {"claimName": pvc_name}},
    ]
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "namespace": ns, "labels": labels},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "standard-rwo",
            "resources": {"requests": {"storage": "10Gi"}},
        },
    }

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
        "metadata": {"name": name, "namespace": ns, "labels": labels},
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
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "selector": {"app.kubernetes.io/name": name},
            # Port 80 is what the BFF preview proxy dials (preview-<id> → :80).
            "ports": [
                {"name": "dev", "port": 80, "targetPort": inputs.dev_port},
                {"name": "editor", "port": 81, "targetPort": inputs.editor_bridge_port},
            ],
        },
    }

    preview_host = f"{name}.{cfg.preview_domain}"  # preview-<id>.tesserix.app
    # Route the preview host to the devai-auth-bff (NOT the Service directly) so
    # the devai_session check gates it; the BFF then proxies to this Service.
    # Gateway is the live tesseract-gateway; the *.tesserix.app wildcard cert
    # covers the single-level preview host.
    virtualservice = {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "VirtualService",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "hosts": [preview_host],
            "gateways": ["istio-ingress/tesseract-gateway"],
            "http": [
                {
                    "route": [
                        {
                            "destination": {
                                "host": "devai-auth-bff.devai.svc.cluster.local",
                                "port": {"number": 8090},
                            }
                        }
                    ],
                    "timeout": "0s",  # HMR WebSocket is long-lived
                }
            ],
        },
    }

    return {
        "pvc": pvc,
        "deployment": deployment,
        "service": service,
        "virtualservice": virtualservice,
        "preview_host": preview_host,  # type: ignore[dict-item]
    }


def build_fullstack_preview_manifests(
    cfg: RuntimeConfig,
    profile: Any,
    *,
    run_id: str,
    repo: str,
    branch: str,
    editor_bridge_image: str = "ghcr.io/tesserix/devai/devai-editor-bridge:main",
    editor_bridge_port: int = 7681,
) -> dict[str, Any]:
    """Materialize a Phase-2 PreviewProfile into a single full-stack pod.

    One pod, one shared PVC workspace, N containers from ``profile.services``
    (frontend + optional backend + optional ephemeral postgres) plus a
    git-clone init and the editor-bridge. Frontend → Service ``preview-<id>``
    (BFF host ``preview-<id>.tesserix.app``); backend → Service ``api-<id>``
    (``api-<id>.tesserix.app``). Both VirtualServices route to devai-auth-bff.
    ${API_PUBLIC_URL}/${WEB_PUBLIC_URL} placeholders in the profile env are
    resolved to the per-session hosts here (the auto API↔UI wiring).
    """
    frag = _dns_safe(run_id, max_len=24)
    web_name = f"preview-{frag}"
    api_name = f"api-{frag}"
    ns = cfg.preview_namespace
    pvc_name = f"{web_name}-work"
    web_host = f"{web_name}.{cfg.preview_domain}"
    api_host = f"{api_name}.{cfg.preview_domain}"
    web_public = f"https://{web_host}"
    api_public = f"https://{api_host}"

    base_labels = {
        "app.kubernetes.io/managed-by": "devai",
        "app.kubernetes.io/name": web_name,
        "devai.tesserix.app/run-id": frag,
        "devai.tesserix.app/role": "preview",
        "devai.tesserix.app/repo": _dns_safe(repo, max_len=48),
    }

    def _resolve(v: str) -> str:
        return v.replace("${API_PUBLIC_URL}", api_public).replace("${WEB_PUBLIC_URL}", web_public)

    volumes = [{"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}}]
    init_containers = [
        {
            "name": "git-clone",
            "image": "alpine/git:2.43.0",
            "command": [
                "sh",
                "-lc",
                "test -d /work/.git || git clone --branch ${BRANCH} --depth 1 "
                "https://x-access-token:${DEVAI_SCM_TOKEN}@github.com/${REPO}.git /work; "
                "chown -R 1000:1000 /work",
            ],
            "env": [
                {"name": "REPO", "value": repo},
                {"name": "BRANCH", "value": branch},
                {
                    "name": "DEVAI_SCM_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": "devai-github-pat", "key": "token", "optional": True}},
                },
            ],
            "volumeMounts": [{"name": "workspace", "mountPath": "/work"}],
        }
    ]

    containers: list[dict[str, Any]] = []
    fe_port = 3000
    be_port = 0
    has_db = False
    for s in profile.services:
        env = [{"name": k, "value": _resolve(v)} for k, v in (s.env or {}).items()]
        if s.role == "database":
            has_db = True
            containers.append(
                {
                    "name": s.name,
                    "image": s.image,
                    "env": env,
                    "ports": [{"name": "db", "containerPort": s.port}],
                    "volumeMounts": [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data"}],
                    "resources": {"requests": {"memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                }
            )
            continue
        wd = f"/work/{s.workdir}" if s.workdir else "/work"
        install_cmd = s.install[-1] if s.install else "true"
        run_cmd = s.run[-1] if s.run else "true"
        containers.append(
            {
                "name": s.name,
                "image": s.image,
                "imagePullPolicy": "IfNotPresent",
                "workingDir": wd,
                "command": ["sh", "-lc", f"cd {wd} && {install_cmd} && exec {run_cmd}"],
                "env": env + [{"name": "HOST", "value": "0.0.0.0"}, {"name": "PORT", "value": str(s.port)}],
                "ports": [{"name": s.role[:15], "containerPort": s.port}],
                "volumeMounts": [{"name": "workspace", "mountPath": "/work"}],
                "readinessProbe": {"tcpSocket": {"port": s.port}, "initialDelaySeconds": 10, "periodSeconds": 5, "failureThreshold": 60},
                "resources": {"requests": {"memory": "768Mi"}, "limits": {"memory": "3Gi"}},
            }
        )
        if s.role == "frontend":
            fe_port = s.port
        elif s.role == "backend":
            be_port = s.port

    configmaps: list[dict[str, Any]] = []
    if has_db:
        volumes.append({"name": "pgdata", "emptyDir": {"sizeLimit": "4Gi"}})
        # Mock-data seeding: a ConfigMap with the schema-aware seeder + a sidecar
        # that waits for the backend to migrate, then inserts realistic fake rows
        # so end-to-end flows return data. Best-effort; never blocks the pod.
        from devai.preview.seeder import seed_configmap, seed_sidecar

        cm = seed_configmap(web_name, ns, base_labels)
        configmaps.append(cm)
        volumes.append({"name": "seed-script", "configMap": {"name": cm["metadata"]["name"]}})
        containers.append(seed_sidecar(cm["metadata"]["name"]))

    # Editor-bridge (shared workspace) for the chat hot-edit loop.
    containers.append(
        {
            "name": "editor-bridge",
            "image": editor_bridge_image,
            "imagePullPolicy": "IfNotPresent",
            "workingDir": "/work",
            "command": ["/usr/local/bin/start-bridge.sh"],
            "env": [{"name": "EDITOR_PORT", "value": str(editor_bridge_port)}],
            "ports": [{"name": "editor", "containerPort": editor_bridge_port}],
            "volumeMounts": [{"name": "workspace", "mountPath": "/work"}],
            "resources": {"requests": {"memory": "256Mi"}, "limits": {"memory": "1Gi"}},
        }
    )

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": web_name, "namespace": ns, "labels": base_labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": web_name}},
            "template": {
                "metadata": {"labels": base_labels},
                "spec": {
                    "serviceAccountName": cfg.service_account_name,
                    "securityContext": cfg.pod_security_context,
                    "initContainers": init_containers,
                    "containers": containers,
                    "volumes": volumes,
                    **({"imagePullSecrets": [{"name": cfg.pull_secret_name}]} if cfg.pull_secret_name else {}),
                },
            },
        },
    }

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name, "namespace": ns, "labels": base_labels},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "standard-rwo",
            "resources": {"requests": {"storage": "12Gi"}},
        },
    }

    def _svc(name: str, target: int) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/name": web_name}},
            "spec": {
                "selector": {"app.kubernetes.io/name": web_name},
                "ports": [{"name": "http", "port": 80, "targetPort": target}],
            },
        }

    def _vs(name: str, host: str) -> dict[str, Any]:
        return {
            "apiVersion": "networking.istio.io/v1beta1",
            "kind": "VirtualService",
            "metadata": {"name": name, "namespace": ns, "labels": base_labels},
            "spec": {
                "hosts": [host],
                "gateways": ["istio-ingress/tesseract-gateway"],
                "http": [
                    {
                        "route": [
                            {"destination": {"host": "devai-auth-bff.devai.svc.cluster.local", "port": {"number": 8090}}}
                        ],
                        "timeout": "0s",
                    }
                ],
            },
        }

    # The web Service must front the FE if present, else the BE (backend-only repos).
    services = [_svc(web_name, fe_port if profile.frontend() else be_port or fe_port)]
    virtualservices = [_vs(web_name, web_host)]
    if be_port and profile.frontend():
        services.append(_svc(api_name, be_port))
        virtualservices.append(_vs(api_name, api_host))

    return {
        "pvc": pvc,
        "configmaps": configmaps,
        "deployment": deployment,
        "services": services,
        "virtualservices": virtualservices,
        "web_host": web_host,
        "api_host": api_host if (be_port and profile.frontend()) else "",
        "preview_host": web_host,  # alias for apply_preview compatibility
    }


__all__ = [
    "PreviewInputs",
    "RunnerJobInputs",
    "build_fullstack_preview_manifests",
    "build_job_spec",
    "build_preview_manifests",
]
