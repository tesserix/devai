"""FastAPI routes for the Fiber-style blueprint runtime.

Mounted at `/api/pipeline/*` by `devai.webhook.app.create_app`. Reads
`app.state.pipeline_service` — the singleton PipelineService stood up on
startup. All routes degrade gracefully when the service is disabled
(`settings.pipeline_enabled=False`): they return 503 with a hint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _service(request: Request):
    svc = getattr(request.app.state, "pipeline_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="pipeline runtime not enabled — set DEVAI_PIPELINE_ENABLED=true",
        )
    return svc


# ────────────────────────────────────────────────────────────────────
# Reads
# ────────────────────────────────────────────────────────────────────


@router.get("/blueprints")
async def list_blueprints(request: Request) -> list[dict[str, Any]]:
    return _service(request).list_blueprints()


@router.get("/blueprints/{name}")
async def get_blueprint_graph(request: Request, name: str) -> dict[str, Any]:
    """Render-ready graph for one blueprint — `{name, title, description,
    lanes, nodes, edges, levels}`. The dashboards render the pipeline flow
    directly from this, so a new (or UI-created) blueprint shows up with no
    UI code change."""
    graph = _service(request).get_blueprint_graph(name)
    if graph is None:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
    return graph


@router.get("/stages")
async def list_stages(request: Request) -> list[str]:
    return _service(request).list_stage_keys()


@router.get("/runs")
async def list_runs(
    request: Request,
    limit: int = 50,
    blueprint: str | None = None,
    repo: str | None = None,
    source: str = "auto",
) -> list[dict[str, Any]]:
    """List recent pipeline tasks.

    `source` — "auto" (in-memory then Redis), "memory" (in-memory only),
    or "persisted" (Redis only). Defaults to auto.
    """
    svc = _service(request)
    if source == "memory":
        return svc.list_tasks_in_memory()[:limit]
    if source == "persisted":
        return await svc.list_persisted_tasks(limit=limit, blueprint=blueprint, repo=repo)

    # auto: merge in-memory + persisted with in-memory winning on id collision
    in_mem = svc.list_tasks_in_memory()
    seen = {t["id"] for t in in_mem}
    persisted = await svc.list_persisted_tasks(limit=limit, blueprint=blueprint, repo=repo)
    out = in_mem[:limit] + [t for t in persisted if t["id"] not in seen]
    return out[:limit]


@router.get("/runs/{task_id}")
async def get_run(request: Request, task_id: str) -> dict[str, Any]:
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    return task


@router.get("/events/recent")
async def recent_events(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    """Return the last `limit` stage events from the ring buffer."""
    return _service(request).recent_events(limit=limit)


# ────────────────────────────────────────────────────────────────────
# Dispatch
# ────────────────────────────────────────────────────────────────────


class DispatchBody(BaseModel):
    """Request body for POST /api/pipeline/runs."""

    intent: str = Field(..., description="Free-form intent / requirements text")
    blueprint: str | None = Field(None, description="Blueprint name; default from settings.pipeline_default_blueprint")
    repo: str = Field("", description="Target repo (org/repo)")
    trigger_type: str = Field("manual", description="manual | webhook | cron | slack")
    label: str = Field("", description="Optional human-readable label")
    agent_context: dict[str, Any] = Field(default_factory=dict)


class DispatchResponse(BaseModel):
    task_id: str
    blueprint: str
    state: str


@router.post("/runs", response_model=DispatchResponse, status_code=202)
async def dispatch_run(request: Request, body: DispatchBody) -> DispatchResponse:
    svc = _service(request)
    try:
        task_id = await svc.dispatch(
            intent=body.intent,
            blueprint=body.blueprint,
            repo=body.repo,
            trigger_type=body.trigger_type,
            label=body.label,
            agent_context=body.agent_context,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e

    snapshot = svc.get_task_in_memory(task_id) or {}
    return DispatchResponse(
        task_id=task_id,
        blueprint=snapshot.get("blueprint", body.blueprint or ""),
        state=snapshot.get("state", "queued"),
    )


# ────────────────────────────────────────────────────────────────────
# SSE stream
# ────────────────────────────────────────────────────────────────────


@router.get("/events/stream")
async def stream_events(request: Request, replay: int = 0):
    """Server-Sent Events feed of stage events.

    Each event is JSON-encoded under the `data:` field. Reconnecting
    clients can pass `?replay=200` to get the last 200 buffered events.
    """
    svc = _service(request)

    async def iterator():
        # Initial hello so the browser knows the stream is open.
        yield "event: hello\ndata: {}\n\n"
        try:
            async for ts, task_id, payload in svc.event_stream(replay=replay):
                if await request.is_disconnected():
                    return
                event_id = f"{ts:.6f}-{task_id}"
                data = json.dumps(
                    {"timestamp": ts, "task_id": task_id, **payload},
                    default=str,
                )
                yield f"id: {event_id}\nevent: stage\ndata: {data}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


__all__ = ["router"]
