"""PreviewService — start / inspect / stop on-demand preview environments."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime
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

# Idle-TTL reaper defaults (CODE-11). A preview is reaped when it hasn't been
# accessed for this long; the loop wakes on the shorter of the interval and a
# 5-minute floor so a long TTL doesn't make GC sluggish.
_DEFAULT_PREVIEW_TTL_SECONDS = 4 * 60 * 60  # 4h
_REAPER_MAX_INTERVAL_SECONDS = 5 * 60


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
        self._reaper_task: asyncio.Task[None] | None = None

    def _runtime(self) -> Any | None:
        return getattr(self._pipeline, "k8s_runtime", None) if self._pipeline is not None else None

    def _ttl_seconds(self) -> int:
        """Idle TTL for previews. Reads the setting if present, else the env,
        else the 4h default — works even when ``config.py`` lacks the field."""
        val = getattr(self._settings, "preview_ttl_seconds", None)
        if val is None:
            raw = os.getenv("DEVAI_PREVIEW_TTL_SECONDS")
            if raw:
                with contextlib.suppress(ValueError):
                    val = int(raw)
        try:
            ttl = int(val) if val is not None else _DEFAULT_PREVIEW_TTL_SECONDS
        except (TypeError, ValueError):
            ttl = _DEFAULT_PREVIEW_TTL_SECONDS
        return ttl if ttl > 0 else _DEFAULT_PREVIEW_TTL_SECONDS

    # Candidate workdirs probed for stack markers (mirror profile resolver).
    _MARKER_DIRS = (
        "",
        "apps/web",
        "apps/frontend",
        "web",
        "frontend",
        "client",
        "packages/web",
        "apps/api",
        "apps/backend",
        "api",
        "backend",
        "server",
        "services/api",
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
            logger.warning(
                "preview: get_repo_tree(%s@%s) failed — falling back to FE default", repo, ref, exc_info=True
            )
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

    # ── Ownership ─────────────────────────────────────────────────────

    @staticmethod
    def _owns(row: dict[str, Any], owner: str | None, *, is_admin: bool = False) -> bool:
        """Whether ``owner`` may act on this preview row.

        Admins see everything; otherwise the row's ``owner`` must match. An
        unresolved owner (None) never matches a row, so an anonymous caller
        only ever sees the per-request previews it started (CODE-10).
        """
        if is_admin:
            return True
        if owner is None:
            return False
        return str(row.get("owner", "")) == owner

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self, repo: str, ref: str = "main", owner: str = "operator") -> dict[str, Any]:
        """Start (or reuse) a preview for (repo, ref, owner)."""
        repo = (repo or "").strip()
        ref = (ref or "main").strip()
        if not repo:
            raise PreviewError("repo is required")

        # Reuse a live session for the same target — the "already running →
        # just attach" fast path. Confirm the Deployment still exists before
        # handing back a reuse hit, otherwise a torn-down (but not-yet-stopped)
        # row would point the iframe at a dead host (CODE-11 / DASH-13).
        existing = await self._db.find_live_preview(repo, ref, owner)
        if existing and await self._deployment_alive(existing.get("deployment")):
            await self._db.touch_preview_session(existing["id"])
            return _public(existing)
        if existing:
            # Stale row — mark it stopped so it stops being a reuse candidate
            # and the reaper/list views reflect reality, then fall through to a
            # fresh start.
            with contextlib.suppress(Exception):
                await self._db.set_preview_session_status(existing["id"], "stopped")
            logger.info("preview: reuse candidate %s has no live deployment — starting fresh", existing.get("id"))

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
        except Exception as e:  # noqa: BLE001
            # DASH-13: log the raw runtime/k8s error (which can carry secret
            # names, image refs, RBAC detail) server-side, but return a generic
            # message to the client so it never leaks infra internals.
            logger.exception("preview %s: failed to start for %s@%s", sid, repo, ref)
            raise PreviewError("failed to start preview — see server logs") from e

        # apply_preview may legitimately return without a deployment name (e.g.
        # a degraded runtime). Guard the keys so a missing field surfaces as a
        # clean PreviewError, not a KeyError 500.
        deployment_name = (applied or {}).get("deployment_name")
        if not deployment_name:
            logger.error("preview %s: apply_preview returned no deployment_name (%r)", sid, applied)
            raise PreviewError("failed to start preview — see server logs")
        fe_url = f"https://{applied['preview_host']}" if applied.get("preview_host") else ""
        await self._db.create_preview_session(sid, repo, ref, owner, fe_url, deployment_name, status="starting")
        logger.info("preview: started %s for %s@%s (owner=%s) -> %s", sid, repo, ref, owner, fe_url)
        return {
            "session_id": sid,
            "repo": repo,
            "ref": ref,
            "owner": owner,
            "fe_url": fe_url,
            "api_url": api_url,
            "preview_url": fe_url,  # alias the dashboard's PreviewPane reads
            "deployment": deployment_name,
            "status": "starting",
        }

    async def get(self, session_id: str, *, owner: str | None = None, is_admin: bool = False) -> dict[str, Any] | None:
        row = await self._db.get_preview_session(session_id)
        # Treat a foreign row exactly like a missing one (404), so an IDOR probe
        # can't distinguish "exists, not yours" from "doesn't exist" (CODE-10).
        if row is None or not self._owns(row, owner, is_admin=is_admin):
            return None
        await self._db.touch_preview_session(session_id)
        return _public(row)

    async def list(self, owner: str | None = None) -> list[dict[str, Any]]:
        return [_public(r) for r in await self._db.list_preview_sessions(owner=owner)]

    async def verify(
        self,
        session_id: str,
        *,
        heal: bool = True,
        owner: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any] | None:
        """Diagnose a preview and (optionally) apply safe in-place fixes.

        Returns the VerifyReport dict (status + per-container diagnoses + the
        list of heals applied), or None when the session is unknown or not
        owned by the caller (CODE-10). Reflects the resolved status back onto
        the session row for the dashboard.
        """
        row = await self._db.get_preview_session(session_id)
        if row is None or not self._owns(row, owner, is_admin=is_admin):
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

    async def stop(self, session_id: str, *, owner: str | None = None, is_admin: bool = False) -> bool:
        row = await self._db.get_preview_session(session_id)
        if row is None or not self._owns(row, owner, is_admin=is_admin):
            return False
        await self._teardown(row)
        return True

    # ── TTL reaper (CODE-11) ──────────────────────────────────────────

    def start_reaper(self) -> None:
        """Launch the idle-TTL reaper as a background task (idempotent).

        Called from the FastAPI lifespan after the service is wired. Safe to
        call when no event loop owns the service yet — it no-ops if a task is
        already running.
        """
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("preview: start_reaper called with no running loop — skipping")
            return
        self._reaper_task = loop.create_task(self._reaper_loop())
        logger.info("preview: TTL reaper started (ttl=%ss)", self._ttl_seconds())

    async def stop_reaper(self) -> None:
        """Cancel the reaper task (called on shutdown)."""
        task = self._reaper_task
        self._reaper_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _reaper_loop(self) -> None:
        """Periodically reap previews idle beyond the TTL."""
        while True:
            ttl = self._ttl_seconds()
            try:
                reaped = await self.reap_expired(ttl_seconds=ttl)
                if reaped:
                    logger.info("preview: reaped %d idle preview(s) (ttl=%ss)", reaped, ttl)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a reap failure must not kill the loop
                logger.warning("preview: reaper pass failed", exc_info=True)
            await asyncio.sleep(min(ttl, _REAPER_MAX_INTERVAL_SECONDS))

    async def reap_expired(self, *, ttl_seconds: int | None = None) -> int:
        """Tear down every non-stopped preview idle beyond the TTL.

        Selects ``status<>'stopped' AND last_access_at < now()-TTL`` (filtered
        in-process over the unscoped session list so it needs no new DB method)
        and runs the standard teardown. Returns the count reaped.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds()
        cutoff = datetime.now(UTC).timestamp() - ttl
        reaped = 0
        rows = await self._db.list_preview_sessions(owner=None, limit=1000)
        for row in rows:
            if str(row.get("status", "")) == "stopped":
                continue
            last = _epoch(row.get("last_access_at")) or _epoch(row.get("created_at"))
            if last is None or last >= cutoff:
                continue
            try:
                await self._teardown(row)
                reaped += 1
            except Exception:  # noqa: BLE001 — keep reaping the rest
                logger.warning("preview: reap teardown failed for %s", row.get("id"), exc_info=True)
        return reaped

    # ── Internals ─────────────────────────────────────────────────────

    async def _teardown(self, row: dict[str, Any]) -> None:
        """Delete the runtime resources for a preview row and mark it stopped."""
        runtime = self._runtime()
        if runtime is not None and row.get("deployment"):
            try:
                await runtime.delete_preview(row["deployment"])
            except Exception:  # noqa: BLE001 — best-effort teardown
                logger.warning("preview: delete_preview(%s) failed", row.get("deployment"), exc_info=True)
        session_id = row.get("id")
        if session_id:
            await self._db.set_preview_session_status(session_id, "stopped")

    async def _deployment_alive(self, name: str | None) -> bool:
        """Whether the preview Deployment still exists (reuse confirmation).

        Uses the runtime's read-only ``get_preview_deployment``. On a degraded
        runtime (no method) we conservatively treat the row as alive so the
        reuse fast-path keeps working in environments without k8s introspection.
        """
        if not name:
            return False
        runtime = self._runtime()
        getter = getattr(runtime, "get_preview_deployment", None)
        if getter is None:
            return True
        try:
            return await getter(name) is not None
        except Exception:  # noqa: BLE001 — read failure shouldn't block reuse
            logger.debug("preview: get_preview_deployment(%s) failed", name, exc_info=True)
            return True


def _epoch(value: Any) -> float | None:
    """Best-effort UTC epoch seconds from a datetime (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["session_id"] = out.get("id")
    out["preview_url"] = out.get("fe_url", "")  # PreviewPane alias
    for k in ("created_at", "updated_at", "last_access_at"):
        v = out.get(k)
        if v is not None and hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out
