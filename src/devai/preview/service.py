"""PreviewService — start / inspect / stop on-demand preview environments."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.services.database import Database

logger = logging.getLogger(__name__)

# Phase-1 FE defaults (mirror devai.pipeline.stages.preview). Phase 2 swaps
# these for a resolved PreviewProfile (per-stack image/command + a backend).
_DEFAULT_DEV_IMAGE = "node:20-bookworm-slim"
_DEFAULT_DEV_PORT = 3000
_DEFAULT_DEV_COMMAND: list[str] = ["sh", "-lc", "npm install && npm run dev -- --host 0.0.0.0 --port 3000"]
_DEFAULT_BRIDGE_PORT = 7681


class PreviewError(ValueError):
    """Raised on invalid preview input or unavailable runtime."""


class PreviewService:
    """Reuses the pipeline's connected K8sJobRuntime + RuntimeConfig so it
    routes/persists previews identically to the spin_preview_pod stage."""

    def __init__(
        self,
        db: Database,
        *,
        pipeline: Any | None = None,
        settings: Settings | None = None,
        scm: Any | None = None,
    ) -> None:
        self._db = db
        self._pipeline = pipeline
        self._settings = settings
        self._scm = scm

    def _runtime(self) -> Any | None:
        return getattr(self._pipeline, "k8s_runtime", None) if self._pipeline is not None else None

    # Candidate workdirs probed for stack markers (mirror profile resolver).
    _MARKER_DIRS = (
        "", "apps/web", "apps/frontend", "web", "frontend", "client", "packages/web",
        "apps/api", "apps/backend", "api", "backend", "server", "services/api",
    )
    _MARKER_FILES = ("package.json", "pyproject.toml", "requirements.txt", "go.mod")

    async def _resolve_repo(self, repo: str, ref: str) -> Any | None:
        """Fetch the repo tree + key files via SCM and resolve a PreviewProfile.

        Returns None on any SCM failure so start() falls back to the default
        FE-only preview rather than erroring.
        """
        if self._scm is None:
            return None
        try:
            tree = await self._scm.get_repo_tree(repo, ref)
        except Exception:  # noqa: BLE001
            logger.warning("preview: get_repo_tree(%s@%s) failed — falling back to FE default", repo, ref, exc_info=True)
            return None
        files = {e.get("path", "") for e in (tree or []) if e.get("path")}
        contents: dict[str, str] = {}
        wanted: set[str] = set()
        for d in self._MARKER_DIRS:
            for k in self._MARKER_FILES:
                p = f"{d}/{k}" if d else k
                if p in files:
                    wanted.add(p)
        for p in wanted:
            try:
                contents[p] = await self._scm.get_file_content(repo, p, ref)
            except Exception:  # noqa: BLE001
                continue
        from devai.preview.profile import resolve_profile

        return resolve_profile(files, contents)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self, repo: str, ref: str = "main", owner: str = "operator") -> dict[str, Any]:
        """Start (or reuse) a preview for (repo, ref, owner)."""
        repo = (repo or "").strip()
        ref = (ref or "main").strip()
        if not repo:
            raise PreviewError("repo is required")

        # Reuse a live session for the same target — the "already running →
        # just attach" fast path.
        existing = await self._db.find_live_preview(repo, ref, owner)
        if existing:
            await self._db.touch_preview_session(existing["id"])
            return _public(existing)

        runtime = self._runtime()
        if runtime is None:
            raise PreviewError("preview runtime unavailable (pipeline/k8s not started)")

        from devai.runtime.job_spec import (
            PreviewInputs,
            build_fullstack_preview_manifests,
            build_preview_manifests,
        )

        sid = uuid.uuid4().hex[:12]
        bridge_image = getattr(self._settings, "editor_bridge_image", "ghcr.io/tesserix/devai/devai-editor-bridge:main")

        # Detect the stack → full-stack (FE + BE + DB) when resolvable; else a
        # plain Node FE default. Detection failures degrade to the default.
        profile = await self._resolve_repo(repo, ref)
        api_url = ""
        try:
            if profile is not None and profile.services:
                manifests = build_fullstack_preview_manifests(
                    runtime.config, profile, run_id=sid, repo=repo, branch=ref, editor_bridge_image=bridge_image
                )
                applied = await runtime.apply_preview(manifests)
                if manifests.get("api_host"):
                    api_url = f"https://{manifests['api_host']}"
                logger.info(
                    "preview %s: resolved profile (%s) -> full-stack",
                    sid,
                    ", ".join(s.name for s in profile.services),
                )
            else:
                inputs = PreviewInputs(
                    run_id=sid,
                    repo=repo,
                    branch=ref,
                    image=_DEFAULT_DEV_IMAGE,
                    dev_command=list(_DEFAULT_DEV_COMMAND),
                    dev_port=_DEFAULT_DEV_PORT,
                    editor_bridge_image=bridge_image,
                    editor_bridge_port=_DEFAULT_BRIDGE_PORT,
                )
                manifests = build_preview_manifests(runtime.config, inputs)
                applied = await runtime.apply_preview(manifests)
                logger.info("preview %s: no stack resolved — FE default", sid)
        except Exception as e:  # noqa: BLE001 — surface a clean error to the route
            raise PreviewError(f"failed to start preview: {e}") from e

        fe_url = f"https://{applied['preview_host']}" if applied.get("preview_host") else ""
        await self._db.create_preview_session(
            sid, repo, ref, owner, fe_url, applied["deployment_name"], status="starting"
        )
        logger.info("preview: started %s for %s@%s (owner=%s) -> %s", sid, repo, ref, owner, fe_url)
        return {
            "session_id": sid,
            "repo": repo,
            "ref": ref,
            "owner": owner,
            "fe_url": fe_url,
            "api_url": api_url,
            "preview_url": fe_url,  # alias the dashboard's PreviewPane reads
            "deployment": applied["deployment_name"],
            "status": "starting",
        }

    async def get(self, session_id: str) -> dict[str, Any] | None:
        row = await self._db.get_preview_session(session_id)
        if row is None:
            return None
        await self._db.touch_preview_session(session_id)
        return _public(row)

    async def list(self, owner: str | None = None) -> list[dict[str, Any]]:
        return [_public(r) for r in await self._db.list_preview_sessions(owner=owner)]

    async def verify(self, session_id: str, *, heal: bool = True) -> dict[str, Any] | None:
        """Diagnose a preview and (optionally) apply safe in-place fixes.

        Returns the VerifyReport dict (status + per-container diagnoses + the
        list of heals applied), or None when the session is unknown. Reflects
        the resolved status back onto the session row for the dashboard.
        """
        row = await self._db.get_preview_session(session_id)
        if row is None:
            return None
        runtime = self._runtime()
        if runtime is None:
            raise PreviewError("preview runtime unavailable (pipeline/k8s not started)")
        name = row.get("deployment")
        if not name:
            raise PreviewError("preview has no deployment to verify")

        from devai.preview.verify import PreviewHealer

        report = await PreviewHealer(runtime).verify(name, heal=heal, web_origin=row.get("fe_url", ""))
        # Map the verify status onto the session lifecycle status.
        mapped = {
            "healthy": "running",
            "healing": "starting",
            "pending": "starting",
            "degraded": "degraded",
            "error": "degraded",
        }.get(report.status, row.get("status", "starting"))
        try:
            await self._db.set_preview_session_status(session_id, mapped)
        except Exception:  # noqa: BLE001 — status reflection is best-effort
            logger.debug("preview: status reflect failed for %s", session_id, exc_info=True)
        out = report.as_dict()
        out["session_id"] = session_id
        return out

    async def stop(self, session_id: str) -> bool:
        row = await self._db.get_preview_session(session_id)
        if row is None:
            return False
        runtime = self._runtime()
        if runtime is not None and row.get("deployment"):
            try:
                await runtime.delete_preview(row["deployment"])
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.warning("preview: delete_preview(%s) failed", row.get("deployment"), exc_info=True)
        await self._db.set_preview_session_status(session_id, "stopped")
        return True


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["session_id"] = out.get("id")
    out["preview_url"] = out.get("fe_url", "")  # PreviewPane alias
    for k in ("created_at", "updated_at", "last_access_at"):
        v = out.get(k)
        if v is not None and hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out
