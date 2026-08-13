"""The sandbox workspace — a filesystem an agent and a person both work in.

A boundary around a run (job.py, isolation.py) says what a run may reach. This
says where it works: one volume at ``/workspace``, shared by the shell, the file
API and later the browser's downloads, so a file produced by one is immediately
visible to the others with no copy step.

Confinement is the whole security surface: a file API that resolves out of
``/workspace`` is not a sandbox, so every path is resolved and checked, symlinks
included.
"""

from __future__ import annotations

import builtins
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devai.sandbox.job import SANDBOX_LABEL

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

WORKSPACE_ROOT = "/workspace"
WORKSPACE_PORT = 8100

_MAX_READ_BYTES = 2_000_000
_SEARCH_MAX_HITS = 200


class WorkspaceError(Exception):
    """A workspace operation that must not be attempted, or a missing path."""


class WorkspaceFiles:
    """Read/write/list/search/replace, confined to one root."""

    def __init__(self, root: Path | str = WORKSPACE_ROOT) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # resolve() follows symlinks, so a link pointing out is caught here too.
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError(f"path {path!r} is outside the workspace")
        return resolved

    def read(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"no such file: {path}")
        if target.stat().st_size > _MAX_READ_BYTES:
            raise WorkspaceError(f"file too large to read: {path}")
        return target.read_text(errors="replace")

    def write(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def list(self, path: str = ".") -> builtins.list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        return [str(p.relative_to(self.root)) for p in sorted(target.iterdir())]

    def search(self, needle: str, path: str = ".") -> builtins.list[dict[str, Any]]:
        target = self._resolve(path)
        hits: list[dict[str, Any]] = []
        for file in sorted(p for p in target.rglob("*") if p.is_file()):
            try:
                lines = file.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, start=1):
                if needle in line:
                    hits.append({"path": str(file.relative_to(self.root)), "line": n, "text": line})
                    if len(hits) >= _SEARCH_MAX_HITS:
                        return hits
        return hits

    def replace(self, path: str, old: str, new: str) -> int:
        content = self.read(path)
        count = content.count(old)
        if count:
            self.write(path, content.replace(old, new))
        return count

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target == self.root:
            raise WorkspaceError("refusing to delete the workspace root")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def run_shell(command: str, *, root: Path | str = WORKSPACE_ROOT, timeout: float = 120.0) -> dict[str, Any]:
    """Run one command with the workspace as its cwd."""
    try:
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
        proc = subprocess.run(  # noqa: S602 — a shell is what this service offers; the pod is the boundary
            command,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": f"command timed out after {timeout}s"}
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


# ── manifests ─────────────────────────────────────────────────────────


def workspace_service_host(sandbox_id: str, *, namespace: str) -> str:
    return f"devai-sandbox-ws-{sandbox_id}.{namespace}.svc.cluster.local:{WORKSPACE_PORT}"


def _meta(sandbox_id: str, namespace: str) -> dict[str, Any]:
    return {
        "name": f"devai-sandbox-ws-{sandbox_id}",
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "devai",
            "app.kubernetes.io/component": "sandbox-workspace",
            SANDBOX_LABEL: sandbox_id,
        },
    }


def build_workspace_manifests(
    record: SandboxRecord,
    *,
    namespace: str,
    token: str,
    image: str = "",
    storage: str = "5Gi",
) -> list[dict[str, Any]]:
    """The workspace objects for one sandbox, in creation order."""
    sid = record.id
    meta = _meta(sid, namespace)
    name = meta["name"]

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": meta,
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": storage}}},
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": meta,
        "type": "Opaque",
        "stringData": {"token": token},
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta,
        "spec": {
            "restartPolicy": "Never",
            "serviceAccountName": "devai-sandbox",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "fsGroup": 1000},
            "containers": [
                {
                    "name": "workspace",
                    "image": image or _default_image(),
                    "command": ["python", "-m", "devai.sandbox.workspace_server"],
                    "ports": [{"containerPort": WORKSPACE_PORT, "name": "http"}],
                    "env": [
                        {
                            "name": "DEVAI_WORKSPACE_TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": name, "key": "token"}},
                        },
                        {"name": "DEVAI_WORKSPACE_ROOT", "value": WORKSPACE_ROOT},
                        {"name": "DEVAI_SANDBOX_ID", "value": sid},
                    ],
                    "volumeMounts": [{"name": "workspace", "mountPath": WORKSPACE_ROOT}],
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "512Mi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": name}}],
        },
    }
    seed = _seed_container(record, secret_name=name, image=image or _default_image())
    if seed is not None:
        pod["spec"]["initContainers"] = [seed]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": meta,
        "spec": {
            "type": "ClusterIP",
            "selector": {SANDBOX_LABEL: sid, "app.kubernetes.io/component": "sandbox-workspace"},
            "ports": [{"port": WORKSPACE_PORT, "targetPort": WORKSPACE_PORT, "name": "http"}],
        },
    }
    return [pvc, secret, pod, service]


def _seed_container(record: SandboxRecord, *, secret_name: str, image: str) -> dict[str, Any] | None:
    """Put the repo in the workspace at its pinned ref, leaving no credential behind.

    The token is passed as a per-command header rather than in the remote URL:
    the agent can read every file under the workspace root, and a URL-embedded
    token would be sitting in ``.git/config`` for it to find.
    """
    repo = record.spec.repo
    if repo is None:
        return None
    script = (
        'auth="Authorization: Basic $(printf "x-access-token:%s" "$DEVAI_GIT_TOKEN" | base64 | tr -d "\\n")"; '
        f'git -c http.extraheader="$auth" clone --no-checkout "{repo.url}" "{WORKSPACE_ROOT}/repo" && '
        f'cd "{WORKSPACE_ROOT}/repo" && '
        f'git -c http.extraheader="$auth" fetch --depth 1 origin "{repo.ref}" && '
        "git checkout FETCH_HEAD"
    )
    return {
        "name": "seed",
        "image": image,
        "command": ["sh", "-ec"],
        "args": [script],
        "env": [
            {"name": "DEVAI_GIT_TOKEN", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "git_token"}}},
        ],
        "volumeMounts": [{"name": "workspace", "mountPath": WORKSPACE_ROOT}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def _default_image() -> str:
    """The runner image — the workspace server ships in the same package."""
    import os

    return os.getenv("DEVAI_RUNNER_IMAGE", "ghcr.io/tesserix/devai/devai-runner:latest")


__all__ = [
    "WORKSPACE_PORT",
    "WORKSPACE_ROOT",
    "WorkspaceError",
    "WorkspaceFiles",
    "build_workspace_manifests",
    "run_shell",
    "workspace_service_host",
]
