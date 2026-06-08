"""Verify & self-heal for live previews.

A preview spins up FE + BE + DB from an unknown repo, so bring-up fails in
predictable, classifiable ways. This module turns "pod won't come up" into a
diagnosis and, where it's safe, an automatic in-place fix — the property that
makes the preview work for messy real-world repos, not just clean ones.

Three layers, deliberately split so the logic is unit-testable without a
cluster:

  * ``diagnose(pod, logs)`` — PURE. Pod status + per-container logs -> a list
    of :class:`Diagnosis`. No K8s calls.
  * ``build_heal_patches(deployment, services, diagnoses, web_origin)`` — PURE.
    Diagnoses -> strategic-merge patches (memory bump, port re-point, CORS env,
    clean reinstall). No K8s calls.
  * :class:`PreviewHealer` — the only stateful piece: reads pod/logs/deployment
    via the runtime, calls the two pure functions, applies the patches.

Heal classes (only the safe, deployment-local ones auto-apply):

  * OOM          -> double the container memory limit (capped).
  * PORT drift   -> dev server bound a different port (Vite "port in use" ->
                    5174); re-point containerPort/PORT/probe + Service targetPort.
  * CORS         -> backend rejected the preview origin; inject permissive CORS
                    env vars (origin-scoped).
  * INSTALL fail -> prepend a clean-reinstall to the container command so the
                    next rollout wipes node_modules/caches and retries.
  * IMAGE_PULL / CRASH / UNKNOWN -> reported, not auto-fixed (no safe local fix).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.runtime.k8s_client import K8sJobRuntime

logger = logging.getLogger(__name__)

# ── Failure classes ──────────────────────────────────────────────────
HEALTHY = "healthy"
PENDING = "pending"
OOM = "oom"
PORT_MISMATCH = "port_mismatch"
CORS = "cors"
INSTALL_FAILED = "install_failed"
IMAGE_PULL = "image_pull"
CRASHLOOP = "crashloop"
UNKNOWN = "unknown"

_HEALABLE = {OOM, PORT_MISMATCH, CORS, INSTALL_FAILED}

# Memory ceiling for OOM bumps — never grow a preview container past this.
_MEM_CAP_BYTES = 6 * 1024**3

# Log signatures. Kept conservative so we don't mis-heal a healthy pod.
_INSTALL_SIGS = (
    "npm err!",
    "eresolve unable to resolve",
    "cannot find module",
    "module not found: error",
    "modulenotfounderror",
    "no matching distribution found",
    "error: could not find a version",
    "could not resolve dependency",
    "err_pnpm",
    "go: updates to go.mod needed",
    "go: cannot find main module",
    "failed to compile",
)
_CORS_SIGS = (
    "has been blocked by cors policy",
    "no 'access-control-allow-origin'",
    "access-control-allow-origin",
    "cors error",
)
_PORT_IN_USE_SIGS = ("eaddrinuse", "address already in use", "port is already in use", "is in use, trying")
# Capture the port a dev server actually bound, e.g. Vite "Local: http://localhost:5174/".
_BOUND_PORT_RE = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})")


@dataclass(slots=True)
class Diagnosis:
    """One container's verdict + (optional) heal action."""

    container: str
    klass: str
    detail: str = ""
    action: dict[str, Any] = field(default_factory=lambda: {"type": "none"})

    @property
    def healable(self) -> bool:
        return self.klass in _HEALABLE and self.action.get("type", "none") != "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "class": self.klass,
            "detail": self.detail,
            "action": self.action,
            "healable": self.healable,
        }


# ── Pure diagnosis ───────────────────────────────────────────────────


def _container_statuses(pod: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize pod.status.containerStatuses into plain dicts.

    Tolerates both camelCase (raw API) and snake_case (``V1Pod.to_dict()``).
    """
    status = pod.get("status") or {}
    raw = status.get("container_statuses") or status.get("containerStatuses") or []
    out: list[dict[str, Any]] = []
    for cs in raw:
        state = cs.get("state") or {}
        last = cs.get("last_state") or cs.get("lastState") or {}

        def _leg(s: dict[str, Any], key: str) -> dict[str, Any]:
            return (s.get(key) or {}) if isinstance(s, dict) else {}

        waiting = _leg(state, "waiting")
        running = _leg(state, "running")
        terminated = _leg(state, "terminated")
        last_term = _leg(last, "terminated")
        out.append(
            {
                "name": cs.get("name", "?"),
                "ready": bool(cs.get("ready")),
                "restart_count": int(cs.get("restart_count") or cs.get("restartCount") or 0),
                "waiting_reason": waiting.get("reason", ""),
                "waiting_message": waiting.get("message", ""),
                "terminated_reason": terminated.get("reason", ""),
                "last_terminated_reason": last_term.get("reason", ""),
                "running": bool(running),
            }
        )
    return out


def _bound_port(logs: str) -> int | None:
    """The last port a dev server reported listening on, if any."""
    found = _BOUND_PORT_RE.findall(logs or "")
    if not found:
        return None
    try:
        return int(found[-1])
    except ValueError:
        return None


def classify(cs: dict[str, Any], logs: str, *, declared_port: int | None = None) -> Diagnosis:
    """Classify a single container from its status + logs (pure)."""
    name = cs["name"]
    low = (logs or "").lower()

    # OOM — the kernel killed it; clearest signal, check first.
    if "OOMKilled" in (cs["terminated_reason"], cs["last_terminated_reason"]):
        return Diagnosis(name, OOM, "container OOMKilled", {"type": "bump_memory"})

    # Image can't be pulled — nothing we can patch locally.
    if cs["waiting_reason"] in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        return Diagnosis(name, IMAGE_PULL, cs["waiting_reason"], {"type": "none"})

    crashing = cs["waiting_reason"] == "CrashLoopBackOff" or cs["restart_count"] > 0
    has_error_logs = any(sig in low for sig in _INSTALL_SIGS)

    # Install/build failure — clean reinstall on next rollout often fixes it.
    if has_error_logs:
        return Diagnosis(name, INSTALL_FAILED, "dependency install/compile failure in logs", {"type": "reinstall"})

    # Dev server picked a different port than we declared (Vite/CRA fallback).
    if any(sig in low for sig in _PORT_IN_USE_SIGS):
        bound = _bound_port(logs)
        if bound and declared_port and bound != declared_port:
            return Diagnosis(
                name, PORT_MISMATCH, f"bound :{bound}, expected :{declared_port}", {"type": "set_port", "port": bound}
            )
        return Diagnosis(name, PORT_MISMATCH, "port-in-use reported", {"type": "none"})

    # CORS — backend up but rejecting the preview origin.
    if any(sig in low for sig in _CORS_SIGS):
        return Diagnosis(name, CORS, "CORS rejection in logs", {"type": "add_cors"})

    if cs["ready"]:
        return Diagnosis(name, HEALTHY)
    if cs["waiting_reason"] in ("ContainerCreating", "PodInitializing", ""):
        return Diagnosis(name, PENDING, cs["waiting_reason"] or "starting")
    if crashing:
        return Diagnosis(name, CRASHLOOP, cs["waiting_reason"] or f"restarts={cs['restart_count']}", {"type": "none"})
    return Diagnosis(name, UNKNOWN, cs["waiting_reason"] or cs["terminated_reason"] or "not ready")


def _declared_ports(deployment: dict[str, Any] | None) -> dict[str, int]:
    """container name -> first declared containerPort, from a Deployment dict."""
    if not deployment:
        return {}
    spec = (((deployment.get("spec") or {}).get("template") or {}).get("spec")) or {}
    ports: dict[str, int] = {}
    for c in spec.get("containers") or []:
        cps = c.get("ports") or []
        if cps:
            p = cps[0].get("container_port") or cps[0].get("containerPort")
            if p:
                ports[c.get("name", "?")] = int(p)
    return ports


def diagnose(
    pod: dict[str, Any] | None,
    logs_by_container: dict[str, str],
    *,
    deployment: dict[str, Any] | None = None,
) -> list[Diagnosis]:
    """Pod status + per-container logs -> per-container diagnoses (pure)."""
    if pod is None:
        return [Diagnosis("*", PENDING, "no pod scheduled yet")]
    declared = _declared_ports(deployment)
    diags: list[Diagnosis] = []
    for cs in _container_statuses(pod):
        diags.append(classify(cs, logs_by_container.get(cs["name"], ""), declared_port=declared.get(cs["name"])))
    if not diags:
        phase = (pod.get("status") or {}).get("phase", "")
        return [Diagnosis("*", PENDING, f"pod phase={phase or 'Unknown'}")]
    return diags


# ── Pure patch construction ──────────────────────────────────────────

_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?)?\s*$")
_MEM_UNITS = {
    "": 1,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def _parse_mem(s: str | None) -> int | None:
    if not s:
        return None
    m = _MEM_RE.match(str(s))
    if not m:
        return None
    return int(float(m.group(1)) * _MEM_UNITS.get(m.group(2) or "", 1))


def _fmt_mem(b: int) -> str:
    if b % (1024**3) == 0:
        return f"{b // 1024**3}Gi"
    return f"{max(1, b // 1024**2)}Mi"


def _find_container(deployment: dict[str, Any], name: str) -> dict[str, Any] | None:
    spec = (((deployment.get("spec") or {}).get("template") or {}).get("spec")) or {}
    for c in spec.get("containers") or []:
        if c.get("name") == name:
            return c
    return None


def _container_role(deployment: dict[str, Any], name: str) -> str:
    """Best-effort role of a container from its declared port name (frontend/backend/...)."""
    c = _find_container(deployment, name)
    if not c:
        return ""
    for p in c.get("ports") or []:
        pn = p.get("name", "")
        if pn in ("frontend", "backend", "database"):
            return pn
    return ""


def build_heal_patches(
    deployment: dict[str, Any],
    services: list[dict[str, Any]],
    diagnoses: list[Diagnosis],
    *,
    web_origin: str = "",
) -> dict[str, Any]:
    """Turn healable diagnoses into in-place patches (pure).

    Returns ``{"deployment": <strategic-merge or None>, "services": [{name,
    targetPort}], "applied": [<human notes>]}``. The Deployment patch only ever
    includes the containers it touches — strategic merge fills the rest.
    """
    container_patches: dict[str, dict[str, Any]] = {}
    service_patches: list[dict[str, Any]] = []
    applied: list[str] = []

    def _cp(name: str) -> dict[str, Any]:
        return container_patches.setdefault(name, {"name": name})

    for d in diagnoses:
        if not d.healable:
            continue
        cur = _find_container(deployment, d.container)
        if cur is None:
            continue
        kind = d.action["type"]

        if kind == "bump_memory":
            lim = ((cur.get("resources") or {}).get("limits") or {}).get("memory")
            cur_b = _parse_mem(lim) or (768 * 1024**2)
            new_b = min(cur_b * 2, _MEM_CAP_BYTES)
            if new_b <= cur_b:
                applied.append(f"{d.container}: already at memory cap ({_fmt_mem(cur_b)}), not bumping")
                continue
            new = _fmt_mem(new_b)
            cp = _cp(d.container)
            cp["resources"] = {"limits": {"memory": new}}
            applied.append(f"{d.container}: memory limit {_fmt_mem(cur_b)} -> {new} (OOM)")

        elif kind == "set_port":
            new_port = int(d.action["port"])
            old_port = None
            cps = cur.get("ports") or []
            if cps:
                old_port = cps[0].get("container_port") or cps[0].get("containerPort")
            cp = _cp(d.container)
            cp["ports"] = [{"name": (cps[0].get("name", "http") if cps else "http"), "containerPort": new_port}]
            cp["readinessProbe"] = {"tcpSocket": {"port": new_port}}
            # Keep PORT env in sync so the next start binds where we expect.
            env = [e for e in (cur.get("env") or []) if e.get("name") != "PORT"]
            env.append({"name": "PORT", "value": str(new_port)})
            cp["env"] = env
            # Re-point the Service whose targetPort matched the old port.
            for svc in services:
                for sp in (svc.get("spec") or {}).get("ports") or []:
                    tp = sp.get("target_port") or sp.get("targetPort")
                    if old_port and tp == old_port:
                        service_patches.append(
                            {
                                "name": svc["metadata"]["name"],
                                "targetPort": new_port,
                                "portName": sp.get("name", "http"),
                            }
                        )
            applied.append(f"{d.container}: re-point port {old_port} -> {new_port}")

        elif kind == "add_cors":
            origin = web_origin or "*"
            cp = _cp(d.container)
            existing = {e.get("name"): e for e in (cur.get("env") or [])}
            # Cover the common framework env var names; harmless if unused.
            for var in ("CORS_ALLOW_ORIGINS", "CORS_ORIGINS", "ALLOWED_ORIGINS", "BACKEND_CORS_ORIGINS"):
                existing[var] = {"name": var, "value": origin}
            cp["env"] = list(existing.values())
            applied.append(f"{d.container}: inject CORS origin {origin}")

        elif kind == "reinstall":
            cmd = cur.get("command") or []
            # The full-stack builder shapes this as ["sh","-lc","cd <wd> && <install> && exec <run>"].
            if len(cmd) == 3 and cmd[0] == "sh":
                clean = (
                    "rm -rf node_modules .next dist .vite 2>/dev/null; npm cache clean --force 2>/dev/null || true; "
                )
                if "rm -rf node_modules" not in cmd[2]:
                    cp = _cp(d.container)
                    cp["command"] = [cmd[0], cmd[1], clean + cmd[2]]
                    applied.append(f"{d.container}: clean reinstall prepended")
                else:
                    applied.append(f"{d.container}: clean reinstall already present, skipping")

    dep_patch: dict[str, Any] | None = None
    if container_patches:
        dep_patch = {"spec": {"template": {"spec": {"containers": list(container_patches.values())}}}}
    return {"deployment": dep_patch, "services": service_patches, "applied": applied}


# ── Stateful orchestration ───────────────────────────────────────────


@dataclass(slots=True)
class VerifyReport:
    name: str
    status: str  # healthy | healing | degraded | pending | error
    diagnoses: list[dict[str, Any]]
    healed: list[str]
    healable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "diagnoses": self.diagnoses,
            "healed": self.healed,
            "healable": self.healable,
        }


class PreviewHealer:
    """Reads a preview's live state and applies safe in-place fixes."""

    def __init__(self, runtime: K8sJobRuntime) -> None:
        self._rt = runtime

    async def _gather(self, name: str) -> tuple[dict | None, dict[str, str], dict | None, list[dict]]:
        pod = await self._rt.get_preview_pod(name)
        deployment = await self._rt.get_preview_deployment(name)
        logs: dict[str, str] = {}
        if pod is not None:
            pod_name = (pod.get("metadata") or {}).get("name")
            for cs in _container_statuses(pod):
                if pod_name:
                    logs[cs["name"]] = await self._rt.preview_pod_logs(pod_name, container=cs["name"])
        # Web + api Services (api-<frag> exists only for full-stack).
        services: list[dict] = []
        for svc_name in (name, "api-" + name[len("preview-") :]) if name.startswith("preview-") else (name,):
            svc = await self._rt.get_preview_service(svc_name)
            if svc:
                services.append(svc)
        return pod, logs, deployment, services

    async def verify(self, name: str, *, heal: bool = True, web_origin: str = "") -> VerifyReport:
        """Diagnose ``name`` and, if ``heal``, apply the safe fixes once."""
        pod, logs, deployment, services = await self._gather(name)
        diags = diagnose(pod, logs, deployment=deployment)
        d_dicts = [d.as_dict() for d in diags]
        healable = any(d.healable for d in diags)

        if all(d.klass == HEALTHY for d in diags):
            return VerifyReport(name, "healthy", d_dicts, [], False)
        if not healable:
            non_pending = [d for d in diags if d.klass not in (HEALTHY, PENDING)]
            status = "degraded" if non_pending else "pending"
            return VerifyReport(name, status, d_dicts, [], False)

        if not heal or deployment is None:
            return VerifyReport(name, "degraded", d_dicts, [], True)

        patches = build_heal_patches(deployment, services, diags, web_origin=web_origin)
        healed: list[str] = []
        if patches["deployment"] is not None:
            if await self._rt.patch_preview_deployment(name, patches["deployment"]):
                healed.extend(patches["applied"])
        for sp in patches["services"]:
            patch = {"spec": {"ports": [{"name": sp["portName"], "port": 80, "targetPort": sp["targetPort"]}]}}
            await self._rt.patch_preview_service(sp["name"], patch)
        status = "healing" if healed else "degraded"
        return VerifyReport(name, status, d_dicts, healed, True)


__all__ = [
    "CORS",
    "CRASHLOOP",
    "Diagnosis",
    "HEALTHY",
    "IMAGE_PULL",
    "INSTALL_FAILED",
    "OOM",
    "PENDING",
    "PORT_MISMATCH",
    "PreviewHealer",
    "UNKNOWN",
    "VerifyReport",
    "build_heal_patches",
    "classify",
    "diagnose",
]
