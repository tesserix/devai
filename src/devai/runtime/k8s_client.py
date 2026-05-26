"""Lightweight async wrapper around `kubernetes_asyncio`.

The wrapper exists so the rest of the codebase imports a stable surface
instead of `kubernetes_asyncio` directly. That lets us:

  * Lazy-load the SDK — `import kubernetes_asyncio` only happens when the
    runtime is enabled. Slim test environments that never touch the
    cluster don't need it installed.
  * Centralise config loading (in-cluster → kubeconfig → fail) and the
    namespace defaulting logic.
  * Make Jobs / Deployments / Services / Pods uniformly accessible from
    one object instead of remembering which `*_api` class owns what.

The runtime is **always optional**. `create_runtime()` returns None when
the SDK isn't installed or cluster auth can't be obtained — callers
degrade to in-process execution. It never crashes the api pod just
because cluster access isn't available.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from devai.runtime.errors import RuntimeNotAvailable, RuntimeNotConfigured

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


# In-cluster default — only used as a hint if the namespace can't be
# read from the downward API at /var/run/secrets/kubernetes.io/serviceaccount/namespace
# which is what pods get for free.
_DEFAULT_NAMESPACE = "devai"


@dataclass(slots=True)
class RuntimeConfig:
    """Resolved cluster-runtime parameters.

    Kept separate from `Settings` so a small subset of fields can be
    passed around without dragging the full pydantic model.
    """

    namespace: str
    runner_image: str
    runner_image_per_stack: dict[str, str]
    preview_domain: str
    registry_url: str
    pull_secret_name: str | None
    service_account_name: str
    default_ttl_seconds: int
    default_backoff_limit: int
    pod_security_context: dict[str, Any]

    @classmethod
    def from_settings(cls, settings: "Settings") -> "RuntimeConfig":
        """Build a RuntimeConfig from the application-wide Settings.

        Reads `k8s_runtime_*` and `runner_*` fields, with conservative
        defaults so a fresh deployment Just Works.
        """
        per_stack = getattr(settings, "runner_image_per_stack", {}) or {}
        if isinstance(per_stack, str):
            # Settings may serialize as JSON-encoded string from env. Parse
            # lazily; an unparseable value falls back to {}.
            import json

            try:
                per_stack = json.loads(per_stack)
            except Exception:  # noqa: BLE001
                per_stack = {}

        return cls(
            namespace=getattr(settings, "k8s_runtime_namespace", _DEFAULT_NAMESPACE) or _DEFAULT_NAMESPACE,
            runner_image=getattr(
                settings,
                "runner_image",
                "ghcr.io/tesserix/devai/devai-runner:main",
            ),
            runner_image_per_stack=per_stack,
            preview_domain=getattr(
                settings,
                "preview_domain",
                "preview.devai.tesserix.app",
            ),
            registry_url=getattr(settings, "registry_url", "") or "",
            pull_secret_name=getattr(settings, "k8s_pull_secret_name", None) or None,
            service_account_name=getattr(
                settings,
                "k8s_runner_service_account",
                "devai-runner",
            ),
            default_ttl_seconds=int(getattr(settings, "k8s_job_ttl_seconds", 3600)),
            default_backoff_limit=int(getattr(settings, "k8s_job_backoff_limit", 0)),
            pod_security_context={"runAsNonRoot": True, "runAsUser": 1000},
        )


class K8sJobRuntime:
    """Thin facade over kubernetes_asyncio Job + Pod + Deployment APIs.

    A single instance is created at `PipelineService.start()` and shared
    across stage handlers via `StageDeps.extra["k8s_runtime"]`. The object
    holds the api-client connection; `close()` must be called at
    shutdown.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        # Lazily set in connect(). Typed as Any to avoid hard-coding the
        # SDK class names at import time.
        self._api_client: Any = None
        self._batch_v1: Any = None
        self._apps_v1: Any = None
        self._core_v1: Any = None
        self._networking_v1: Any = None
        self._connected = False

    # ── lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Load cluster config + open the shared api-client."""
        if self._connected:
            return
        try:
            from kubernetes_asyncio import client, config as k8s_config
        except ImportError as e:
            raise RuntimeNotAvailable(
                "kubernetes_asyncio not installed — add it to dependencies"
            ) from e

        # In-cluster first (pod-mounted SA token). Fall back to kubeconfig
        # for local dev. Failures aren't fatal — surface as
        # RuntimeNotConfigured so the caller can degrade.
        loaded = False
        if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
            try:
                k8s_config.load_incluster_config()
                loaded = True
                logger.info("k8s runtime: loaded in-cluster config")
            except Exception:
                logger.exception("k8s runtime: in-cluster config load failed")

        if not loaded:
            try:
                await k8s_config.load_kube_config()
                loaded = True
                logger.info("k8s runtime: loaded local kubeconfig")
            except Exception as e:
                raise RuntimeNotConfigured(
                    f"no kubernetes config (in-cluster nor kubeconfig): {e}"
                ) from e

        self._api_client = client.ApiClient()
        self._batch_v1 = client.BatchV1Api(self._api_client)
        self._apps_v1 = client.AppsV1Api(self._api_client)
        self._core_v1 = client.CoreV1Api(self._api_client)
        self._networking_v1 = client.NetworkingV1Api(self._api_client)
        # CustomObjectsApi is what Istio's networking.istio.io VirtualService
        # lives under — the typed NetworkingV1Api only covers core resources.
        self._custom_objects = client.CustomObjectsApi(self._api_client)
        self._connected = True

    async def close(self) -> None:
        if self._api_client is not None:
            try:
                await self._api_client.close()
            except Exception:  # noqa: BLE001
                logger.exception("k8s runtime: api-client close failed")
        self._connected = False

    # ── accessors used by stage handlers ─────────────────────────────

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def batch_v1(self) -> Any:
        if not self._connected:
            raise RuntimeNotConfigured("k8s runtime not connected — call connect() first")
        return self._batch_v1

    @property
    def apps_v1(self) -> Any:
        if not self._connected:
            raise RuntimeNotConfigured("k8s runtime not connected")
        return self._apps_v1

    @property
    def core_v1(self) -> Any:
        if not self._connected:
            raise RuntimeNotConfigured("k8s runtime not connected")
        return self._core_v1

    @property
    def networking_v1(self) -> Any:
        if not self._connected:
            raise RuntimeNotConfigured("k8s runtime not connected")
        return self._networking_v1

    # ── high-level helpers ───────────────────────────────────────────

    async def create_job(self, job_spec: Any) -> str:
        """Create a Job in the runtime namespace, return its full name."""
        from devai.runtime.errors import JobDispatchFailed

        try:
            created = await self._batch_v1.create_namespaced_job(
                namespace=self._config.namespace, body=job_spec
            )
        except Exception as e:  # noqa: BLE001 — surface every API error
            raise JobDispatchFailed(f"create_namespaced_job: {e}") from e
        name = created.metadata.name
        logger.info("k8s runtime: created job %s/%s", self._config.namespace, name)
        return name

    async def delete_job(self, name: str, *, propagation: str = "Background") -> None:
        """Best-effort Job deletion — used by stage cleanup and TTL trims."""
        from kubernetes_asyncio import client as k8s_client

        try:
            await self._batch_v1.delete_namespaced_job(
                name=name,
                namespace=self._config.namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy=propagation),
            )
            logger.info("k8s runtime: deleted job %s/%s", self._config.namespace, name)
        except Exception:  # noqa: BLE001
            logger.exception("k8s runtime: delete_job failed for %s", name)

    async def get_job(self, name: str) -> Any:
        """Read a Job's current state."""
        return await self._batch_v1.read_namespaced_job(
            name=name, namespace=self._config.namespace
        )

    async def pod_logs(self, pod_name: str, *, tail_lines: int = 500) -> str:
        """Fetch the (truncated) stdout of a pod. Used for failure triage."""
        try:
            return await self._core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=self._config.namespace,
                tail_lines=tail_lines,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("k8s runtime: pod_logs(%s) failed: %s", pod_name, e)
            return ""

    # ── Preview pod (Deployment + Service + Istio VirtualService) ────
    #
    # These three are applied together by ``apply_preview`` and torn down
    # together by ``delete_preview``. They're keyed by the Deployment name
    # the build_preview_manifests() helper emits, which the preview_spinner
    # agent persists on the DevAITask so a later cleanup stage can find it.

    async def apply_preview(self, manifests: dict[str, Any]) -> dict[str, str]:
        """Apply the {deployment, service, virtualservice} bundle.

        ``manifests`` is the dict returned by
        :func:`devai.runtime.job_spec.build_preview_manifests`. Returns
        ``{deployment_name, service_name, virtualservice_name,
        preview_host, editor_host}`` so the caller can stash it on the
        DevAITask and later resolve cleanup targets.

        Idempotent — if the resources already exist (re-run, retry), we
        patch in place rather than raising AlreadyExists. That keeps the
        preview_spinner agent safely re-entrant.
        """
        from devai.runtime.errors import JobDispatchFailed

        dep = manifests["deployment"]
        svc = manifests["service"]
        vs = manifests["virtualservice"]
        ns = self._config.namespace

        try:
            await self._apps_v1.create_namespaced_deployment(namespace=ns, body=dep)
        except Exception as e:
            # AlreadyExists → patch. We don't import the SDK exception type
            # to keep the import surface narrow; the string check is the
            # documented kubernetes_asyncio escape hatch.
            if "AlreadyExists" in str(e) or " 409 " in str(e):
                try:
                    await self._apps_v1.patch_namespaced_deployment(
                        name=dep["metadata"]["name"], namespace=ns, body=dep
                    )
                except Exception as patch_err:
                    raise JobDispatchFailed(f"patch deployment: {patch_err}") from patch_err
            else:
                raise JobDispatchFailed(f"create deployment: {e}") from e

        try:
            await self._core_v1.create_namespaced_service(namespace=ns, body=svc)
        except Exception as e:
            if "AlreadyExists" in str(e) or " 409 " in str(e):
                try:
                    await self._core_v1.patch_namespaced_service(
                        name=svc["metadata"]["name"], namespace=ns, body=svc
                    )
                except Exception as patch_err:
                    raise JobDispatchFailed(f"patch service: {patch_err}") from patch_err
            else:
                raise JobDispatchFailed(f"create service: {e}") from e

        try:
            await self._custom_objects.create_namespaced_custom_object(
                group="networking.istio.io",
                version="v1beta1",
                namespace=ns,
                plural="virtualservices",
                body=vs,
            )
        except Exception as e:
            if "AlreadyExists" in str(e) or " 409 " in str(e):
                try:
                    await self._custom_objects.patch_namespaced_custom_object(
                        group="networking.istio.io",
                        version="v1beta1",
                        namespace=ns,
                        plural="virtualservices",
                        name=vs["metadata"]["name"],
                        body=vs,
                    )
                except Exception as patch_err:
                    raise JobDispatchFailed(f"patch virtualservice: {patch_err}") from patch_err
            else:
                # Istio might not be installed in dev clusters — surface a
                # warning but don't fail the whole preview. The Service
                # still lets a port-forward work for local testing.
                logger.warning("k8s runtime: virtualservice create failed (%s) — preview reachable only by Service", e)

        name = dep["metadata"]["name"]
        logger.info(
            "k8s runtime: applied preview %s/%s (preview_host=%s editor_host=%s)",
            ns,
            name,
            manifests.get("preview_host"),
            manifests.get("editor_host"),
        )
        return {
            "deployment_name": name,
            "service_name": svc["metadata"]["name"],
            "virtualservice_name": vs["metadata"]["name"],
            "preview_host": manifests.get("preview_host", ""),
            "editor_host": manifests.get("editor_host", ""),
        }

    async def delete_preview(self, name: str) -> None:
        """Tear down a preview pod and its routing. Best-effort.

        Used by the run-cleanup stage when a run completes or is cancelled.
        Each leg is wrapped because deleting one resource shouldn't block
        deleting the others — a half-cleaned preview is worse than no
        cleanup attempt.
        """
        ns = self._config.namespace
        # VirtualService first so traffic stops flowing before the pod dies.
        try:
            await self._custom_objects.delete_namespaced_custom_object(
                group="networking.istio.io",
                version="v1beta1",
                namespace=ns,
                plural="virtualservices",
                name=name,
            )
        except Exception:
            logger.debug("delete virtualservice %s skipped/failed", name, exc_info=True)
        try:
            await self._core_v1.delete_namespaced_service(name=name, namespace=ns)
        except Exception:
            logger.debug("delete service %s skipped/failed", name, exc_info=True)
        try:
            await self._apps_v1.delete_namespaced_deployment(name=name, namespace=ns)
        except Exception:
            logger.debug("delete deployment %s skipped/failed", name, exc_info=True)
        logger.info("k8s runtime: deleted preview %s/%s", ns, name)

    async def find_pod_for_job(self, job_name: str) -> str | None:
        """First pod matching `job-name=<job_name>`. None if the Job hasn't
        scheduled a pod yet (or already cleaned up)."""
        try:
            pods = await self._core_v1.list_namespaced_pod(
                namespace=self._config.namespace,
                label_selector=f"job-name={job_name}",
                limit=1,
            )
        except Exception:  # noqa: BLE001
            logger.exception("k8s runtime: list pods for job %s failed", job_name)
            return None
        if not pods.items:
            return None
        return pods.items[0].metadata.name


# ─────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────


async def create_runtime(settings: "Settings") -> K8sJobRuntime | None:
    """Build + connect a K8sJobRuntime, or return None when impossible.

    None is a normal outcome — the runtime is opt-in via
    `DEVAI_K8S_RUNTIME_ENABLED=true` and gracefully degrades when the SDK
    or cluster auth isn't available. Callers should:

        runtime = await create_runtime(settings)
        if runtime is None:
            # in-process fallback path
            ...
    """
    if not getattr(settings, "k8s_runtime_enabled", False):
        logger.info("k8s runtime: disabled (DEVAI_K8S_RUNTIME_ENABLED is not true)")
        return None

    cfg = RuntimeConfig.from_settings(settings)
    runtime = K8sJobRuntime(cfg)
    try:
        await runtime.connect()
    except RuntimeNotAvailable:
        logger.warning(
            "k8s runtime: kubernetes_asyncio SDK missing — running stages in-process"
        )
        return None
    except RuntimeNotConfigured as e:
        logger.warning("k8s runtime: cluster auth unavailable (%s) — running in-process", e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("k8s runtime: unexpected connect failure — running in-process")
        return None
    return runtime


__all__ = [
    "K8sJobRuntime",
    "RuntimeConfig",
    "create_runtime",
]
