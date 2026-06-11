"""Run-level Repo viewer API.

Powers the dashboard's REPO tab. Read-only — clients can list the file
tree and read file contents but cannot write. Edits happen on the
original repo via SCM PRs; this surface is purely for inspection.

Endpoints:

  GET  /api/runs/{run_id}/repo/tree?path=...
       List entries under a path. Returns ``[{name,type,size,path}]``.

  GET  /api/runs/{run_id}/repo/file?path=...
       Read a file's contents. Returns ``{path, encoding, content, size}``.
       Encoding is "utf-8" for text, "base64" for binary.

  GET  /api/runs/{run_id}/repo/events
       Server-Sent Events stream of file-change events emitted by the
       pipeline as agents modify the workspace. Each event is JSON
       ``{kind: "created|modified|deleted", path, agent, timestamp}``.

Data sources, in order of preference:
  1. Repo write events the pipeline pushes to
     ``devai:run:<run_id>:repo_events`` (Redis list). These are emitted
     by the senior_developer / db_engineer agents whenever they apply
     a file change.
  2. The DevAITask's agent_context['committed_files'] (snapshot).
  3. The remote SCM (GitHub/GitLab/ADO) over the configured branch —
     used to show the live PR view when the workspace isn't cloned
     locally.

The route never trusts the client's ``path``: every path is normalized
and rejected if it escapes the repo root with ``..``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs", "repo"])


_REPO_EVENTS_KEY = "devai:run:{run_id}:repo_events"
_REPO_EVENTS_TTL = 86400 * 7  # 7d — long enough to replay after a refresh

# Max bytes we'll return from /file. Anything bigger gets truncated with a
# flag set on the response — the dashboard can render a "file too large"
# notice instead of hammering Chrome's text-area.
_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB


def _safe_path(path: str) -> str:
    """Normalize and refuse traversal."""
    if not path:
        return ""
    p = PurePosixPath(path)
    if any(part == ".." for part in p.parts):
        raise HTTPException(status_code=400, detail="path traversal not allowed")
    if p.is_absolute():
        # Reject absolute paths — repo content is always relative.
        p = PurePosixPath(*p.parts[1:])
    return str(p).lstrip("/")


async def _live_workbranch(request: Request, run_id: str) -> str:
    """The branch the developer agent is committing to RIGHT NOW.

    The run's ``branch_name`` only lands when the implement stage
    completes — for the (long) duration of the stage, the agent records
    its working branch under ``devai:run:<id>:workbranch`` so the REPO
    tab can follow the commits live instead of showing a stale branch.
    """
    try:
        redis = getattr(request.app.state.state_manager, "redis", None)
        if redis is None:
            return ""
        return str(await redis.get(f"devai:run:{run_id}:workbranch") or "")
    except Exception:  # noqa: BLE001 — visibility aid only
        return ""


async def _resolve_run(request: Request, run_id: str, branch_override: str = "") -> dict[str, Any]:
    """Resolve a run_id to repo+branch context.

    Tries the LangGraph state-manager first (legacy ALMOrchestrator
    runs), falls back to the blueprint runtime's persisted task store
    (DevAITask runs). Returns ``{repo, branch, source}`` or raises 404.

    Branch preference: explicit ``branch_override`` from the client →
    the agent's live working branch → the task's recorded branch.
    """
    state = request.app.state.state_manager
    run = await state.get_run(run_id)
    if run:
        ctx = run.get("context") or {}
        if isinstance(ctx, str):
            with contextlib.suppress(Exception):
                ctx = json.loads(ctx)
        branch = (
            branch_override
            or await _live_workbranch(request, run_id)
            or (ctx.get("branch_name") if isinstance(ctx, dict) else None)
            or ""
        )
        return {
            "repo": (ctx.get("repo_full_name") if isinstance(ctx, dict) else None)
            or run.get("repo_full_name")
            or run.get("repo", ""),
            "branch": branch,
            "source": "alm",
        }

    # Blueprint runtime path.
    svc = getattr(request.app.state, "pipeline_service", None)
    if svc is not None:
        task = await svc.get_task(run_id)
        if task:
            ag = task.get("agent_context") or {}
            branch = (
                branch_override
                or await _live_workbranch(request, run_id)
                or task.get("branch_name")
                or ag.get("branch_name")
                or ag.get("default_branch")
                or ""
            )
            return {
                "repo": task.get("repo") or ag.get("repo", ""),
                "branch": branch,
                "source": "blueprint",
            }

    raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")


@router.get("/{run_id}/repo/tree")
async def list_repo_tree(
    request: Request, run_id: str, path: str = "", branch: str = ""
) -> dict[str, Any]:
    """List entries under ``path`` for the run's repo.

    Returns ``{repo, branch, path, entries: [{name,type,size,path}]}``.
    ``type`` is one of ``file | dir``. The dashboard renders this as a
    file tree on the REPO tab. ``branch`` overrides the resolved branch.
    """
    safe = _safe_path(path)
    ctx = await _resolve_run(request, run_id, branch_override=branch)

    from devai.scm import create_scm_client

    config = request.app.state.config
    scm = create_scm_client(config)
    try:
        entries = await scm.list_files(ctx["repo"], safe, ctx.get("branch") or None)
    except Exception as e:
        logger.warning("repo tree: list_files failed for %s/%s: %s", ctx["repo"], safe, e)
        entries = []
    finally:
        with contextlib.suppress(Exception):
            await scm.close()

    # Normalize each entry — SCM clients return slightly different
    # shapes. We coerce to a single contract here so the frontend
    # doesn't have to branch by provider.
    normalized: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        kind = e.get("type") or ("dir" if e.get("path", "").endswith("/") else "file")
        if kind == "tree":
            kind = "dir"
        if kind == "blob":
            kind = "file"
        normalized.append(
            {
                "name": e.get("name") or PurePosixPath(e.get("path", "")).name,
                "type": kind,
                "size": int(e.get("size", 0) or 0),
                "path": e.get("path", ""),
            }
        )
    normalized.sort(key=lambda r: (r["type"] != "dir", r["name"].lower()))

    return {
        "repo": ctx["repo"],
        "branch": ctx.get("branch", ""),
        "path": safe,
        "entries": normalized,
    }


@router.get("/{run_id}/repo/file")
async def read_repo_file(
    request: Request, run_id: str, path: str, branch: str = ""
) -> dict[str, Any]:
    """Read a single file's contents.

    Read-only — there's no companion PUT. Returns text content directly
    when the bytes decode as UTF-8; otherwise returns base64 so the
    client can preview hex or just display "binary file".
    """
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    safe = _safe_path(path)
    ctx = await _resolve_run(request, run_id, branch_override=branch)

    from devai.scm import create_scm_client

    config = request.app.state.config
    scm = create_scm_client(config)
    try:
        raw = await scm.get_file_content(ctx["repo"], safe, ctx.get("branch") or None)
    except Exception as e:
        logger.info("repo file: get_file_content failed for %s:%s — %s", ctx["repo"], safe, e)
        raise HTTPException(status_code=404, detail=f"file {safe!r} not found in run") from e
    finally:
        with contextlib.suppress(Exception):
            await scm.close()

    # SCM clients sometimes return str (already decoded), sometimes
    # bytes. Handle both — and cap the size we'll return to the browser.
    if isinstance(raw, bytes):
        truncated = len(raw) > _MAX_FILE_BYTES
        payload = raw[:_MAX_FILE_BYTES]
        try:
            content = payload.decode("utf-8")
            return {
                "repo": ctx["repo"],
                "branch": ctx.get("branch", ""),
                "path": safe,
                "encoding": "utf-8",
                "content": content,
                "size": len(raw),
                "truncated": truncated,
            }
        except UnicodeDecodeError:
            return {
                "repo": ctx["repo"],
                "branch": ctx.get("branch", ""),
                "path": safe,
                "encoding": "base64",
                "content": base64.b64encode(payload).decode("ascii"),
                "size": len(raw),
                "truncated": truncated,
            }
    text = str(raw)
    truncated = len(text.encode("utf-8")) > _MAX_FILE_BYTES
    if truncated:
        text = text[:_MAX_FILE_BYTES]
    return {
        "repo": ctx["repo"],
        "branch": ctx.get("branch", ""),
        "path": safe,
        "encoding": "utf-8",
        "content": text,
        "size": len(text),
        "truncated": truncated,
    }


@router.get("/{run_id}/repo/events")
async def stream_repo_events(request: Request, run_id: str, replay: int = 50) -> StreamingResponse:
    """SSE stream of repo file-change events for a single run.

    Events look like::

        {"kind": "modified", "path": "src/app.py", "agent": "senior_developer",
         "triggered_by": "alice@…", "trace_id": "…", "timestamp": 1716...}

    The pipeline pushes events to the Redis list
    ``devai:run:<run_id>:repo_events``; this route tails that list with
    a blocking pop pattern and forwards each new entry as SSE.
    """
    # Verify the run exists up front so the client doesn't sit on an
    # empty stream forever if they typo'd a run id.
    await _resolve_run(request, run_id)

    state = request.app.state.state_manager
    key = _REPO_EVENTS_KEY.format(run_id=run_id)

    async def iterator():
        import time as _time

        # Hello frame so the browser flips EventSource to OPEN.
        yield "event: hello\ndata: {}\n\n"

        # Replay the last N entries first.
        buffered: list[Any] = []
        if replay > 0:
            try:
                buffered = await state.redis.lrange(key, -replay, -1)
            except Exception:
                buffered = []
            for entry in buffered:
                yield f"event: repo\ndata: {entry}\n\n"

        # Tail. Redis BLPOP returns (key, value); we use a short timeout
        # so we can check `is_disconnected()` between blocks instead of
        # holding the connection forever. Keep-alive comments every 15s —
        # without them every intermediate hop (Next rewrite proxy, Istio,
        # Cloudflare) is free to kill the idle stream, which is exactly
        # why the REPO tab badge sat at "offline" while the pipeline SSE
        # (which heartbeats) stayed live.
        cursor = max(0, len(buffered) if replay > 0 else 0)
        last_sent = _time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            try:
                entries = await state.redis.lrange(key, cursor, cursor + 49)
            except Exception:
                entries = []
            if not entries:
                if _time.monotonic() - last_sent >= 15.0:
                    yield ": keep-alive\n\n"
                    last_sent = _time.monotonic()
                await asyncio.sleep(1.0)
                continue
            for entry in entries:
                yield f"event: repo\ndata: {entry}\n\n"
            cursor += len(entries)
            last_sent = _time.monotonic()

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def emit_repo_event(
    redis: Any,
    run_id: str,
    *,
    kind: str,
    path: str,
    agent: str = "",
    triggered_by: str = "",
    trace_id: str = "",
) -> None:
    """Push a repo file-change event for an active run.

    Called by stages / agents whenever they create, modify, or delete a
    file in the working copy. The dashboard's REPO tab tails the list
    via the SSE route above.

    Defensive — never raises. A missing Redis means we lose live
    updates, not the entire run.
    """
    import time

    payload = json.dumps(
        {
            "kind": kind,
            "path": path,
            "agent": agent,
            "triggered_by": triggered_by,
            "trace_id": trace_id,
            "timestamp": time.time(),
        }
    )
    try:
        key = _REPO_EVENTS_KEY.format(run_id=run_id)
        pipe = redis.pipeline()
        pipe.rpush(key, payload)
        pipe.expire(key, _REPO_EVENTS_TTL)
        # Cap list length so a runaway run can't blow up Redis memory.
        pipe.ltrim(key, -5000, -1)
        await pipe.execute()
    except Exception:
        logger.debug("emit_repo_event failed for run=%s path=%s", run_id, path, exc_info=True)


__all__ = ["router", "emit_repo_event"]
