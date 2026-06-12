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

from fastapi import APIRouter, HTTPException, Query, Request
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


async def _require_principal(request: Request):
    """Resolve the caller's Principal, gated by the ``require_auth`` flag.

    Behavior-neutral by default: when ``DEVAI_REQUIRE_AUTH`` is False (the
    current prod default) this returns the resolved principal or ``None`` for
    anonymous callers — exactly the legacy ``extract_principal`` path, so
    nothing changes until the flag is flipped in chart values (CHART-1). When
    ``require_auth`` is True it delegates to :func:`devai.authz.require_principal`,
    which 401s anonymous callers (no fallback to a literal ``operator``).
    """
    config = getattr(request.app.state, "config", None)
    if config is not None and getattr(config, "require_auth", False):
        from devai.authz import require_principal

        return await require_principal(request)
    from devai.identity import extract_principal

    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        return None


async def _authorize_run(request: Request, task: dict[str, Any] | None):
    """Resolve the caller and enforce team membership on the run's owning team.

    Returns the resolved principal (or None when auth is off and the caller is
    anonymous — the behavior-neutral default path). When ``require_auth`` is on,
    :func:`_require_principal` 401s for anonymous callers, and we additionally
    reject callers who are not members of the run's owning team
    (``team_service.can_dispatch``).

    This is the run-control / approval-gate guard for CODE-2: it ensures a
    human-in-the-loop decision is attributable to an authorized team member.
    """
    principal = await _require_principal(request)
    team_id = (task or {}).get("team_id") or ""
    team_service = getattr(request.app.state, "team_service", None)
    if team_service is not None and not team_service.can_dispatch(principal, team_id):
        raise HTTPException(status_code=403, detail=f"not a member of team {team_id}")
    return principal


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
    limit: int = Query(50, ge=1, le=500),
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


@router.get("/runs/{task_id}/a2a")
async def get_run_a2a(request: Request, task_id: str) -> list[dict[str, Any]]:
    """Agent-to-agent message timeline for a run (dashboard TIMELINE tab).

    Sourced from the run's handover bag; returns [] for runs that produced no
    A2A traffic so the dashboard timeline degrades cleanly.
    """
    task = await _service(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    ctx = task.get("agent_context") or {}
    msgs = ctx.get("a2a_messages") or task.get("a2a_messages") or []
    return msgs if isinstance(msgs, list) else []


@router.get("/runs/{task_id}/delegation-plan")
async def get_run_delegation_plan(request: Request, task_id: str) -> dict[str, Any] | None:
    """The advisory supervisor delegation plan for a run, if any.

    The supervisor stage writes ``delegation_plan`` into the handover bag. It is
    ADVISORY metadata — it does not add/remove/reorder the executed DAG stages —
    and is surfaced read-only on the run detail page. Returns null when the run
    didn't pass through a supervisor stage.
    """
    task = await _service(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    plan = (task.get("agent_context") or {}).get("delegation_plan")
    return plan if isinstance(plan, dict) else None


@router.get("/events/recent")
async def recent_events(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Return the last `limit` stage events from the ring buffer."""
    return _service(request).recent_events(limit=limit)


@router.get("/runs/{task_id}/events")
async def get_run_events(
    request: Request, task_id: str, limit: int = Query(500, ge=1, le=2000)
) -> list[dict[str, Any]]:
    """Durable per-run event log (stage + agent_status + a2a envelopes).

    Sourced from the Redis log the event hub appends to — unlike the
    in-process ring it survives pod restarts. Falls back to the task's
    recorded stage_events when the log is missing (older runs / no Redis).
    """
    svc = _service(request)
    redis = getattr(svc.state_manager, "redis", None)
    if redis is not None:
        try:
            raw = await redis.lrange(f"devai:run:{task_id}:events", -limit, -1)
            if raw:
                return [json.loads(r) for r in raw]
        except Exception:  # noqa: BLE001 — fall through to the task snapshot
            pass
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    events = task.get("stage_events") or []
    return [{"event_type": "stage", "task_id": task_id, **e} for e in events[-limit:]]


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
    # Teams (Phase 2/3) — both optional; empty = unscoped.
    team_id: str = Field("", description="Human team that owns this run")
    crew_id: str = Field("", description="AI agent crew assigned to this run")
    # Composer context (Phase 1/4) — @-mentioned files/symbols/urls + image keys.
    context_refs: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list, description="object_store keys for uploaded images")


class DispatchResponse(BaseModel):
    task_id: str
    blueprint: str
    state: str
    team_id: str = ""


@router.post("/runs", response_model=DispatchResponse, status_code=202)
async def dispatch_run(request: Request, body: DispatchBody) -> DispatchResponse:
    from devai.identity import trace_id_from_request

    svc = _service(request)
    principal = await _require_principal(request)

    # Resolve the owning team: explicit body wins, else the caller's primary
    # team. Authorize the choice against the caller's memberships.
    team_id = body.team_id or (principal.primary_team_id if principal else "")
    team_service = getattr(request.app.state, "team_service", None)
    if team_service is not None and not team_service.can_dispatch(principal, team_id):
        raise HTTPException(status_code=403, detail=f"not a member of team {team_id}")

    # Fold composer context into the handover bag so the AgentRunner can
    # hydrate @-mentions + image attachments.
    agent_context = dict(body.agent_context)
    if body.context_refs:
        agent_context["context_refs"] = body.context_refs
    if body.attachments:
        agent_context["attachments"] = body.attachments

    try:
        task_id = await svc.dispatch(
            intent=body.intent,
            blueprint=body.blueprint,
            repo=body.repo,
            trigger_type=body.trigger_type,
            label=body.label,
            agent_context=agent_context,
            principal=principal.to_dict() if principal else None,
            trace_id=trace_id_from_request(request),
            team_id=team_id,
            crew_id=body.crew_id,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e

    snapshot = svc.get_task_in_memory(task_id) or {}
    return DispatchResponse(
        task_id=task_id,
        blueprint=snapshot.get("blueprint", body.blueprint or ""),
        state=snapshot.get("state", "queued"),
        team_id=team_id,
    )


# ────────────────────────────────────────────────────────────────────
# Dashboard trigger / retrigger / run-control (durable, via the pipeline)
#
# These replace the legacy ALMOrchestrator-backed handlers at
# /dashboard/api/pipeline/* (which the Next.js rewrite never reaches, since
# /api/pipeline/* maps here). Implementing them on the pipeline router makes
# the dashboard's Start / Retry / Pause / Resume / Stop buttons run through the
# durable pipeline (Temporal when DEVAI_WORKFLOW_PROVIDER=temporal).
# ────────────────────────────────────────────────────────────────────


class TriggerBody(BaseModel):
    """Dashboard "Start Pipeline" body (dashboard/src/lib/api.ts triggerPipeline)."""

    repo: str = ""
    requirements: str = ""
    issue_number: int | None = None
    blueprint: str | None = None
    # "full" → gates self-approve, run flows end-to-end (default);
    # "gated" → pause at every approval gate for a human decision.
    autonomy: str | None = None
    # "Brainstorm first": run the boardroom debate stage (condition:
    # output.brainstorm in the blueprint) before planning/stories — the
    # panel's agreed decision feeds every downstream agent.
    brainstorm: bool = False


@router.post("/trigger")
async def trigger(request: Request, body: TriggerBody) -> dict[str, Any]:
    """Dashboard "Start Pipeline" — dispatch a durable blueprint run."""
    from devai.identity import trace_id_from_request

    svc = _service(request)
    intent = (body.requirements or "").strip()
    if not intent:
        raise HTTPException(status_code=400, detail="requirements text is required")
    principal = await _require_principal(request)
    agent_context: dict[str, Any] = {"requirements": intent}
    if body.issue_number is not None:
        agent_context["trigger_ref"] = str(body.issue_number)
    if body.brainstorm:
        agent_context["brainstorm"] = True
    try:
        task_id = await svc.dispatch(
            intent=intent,
            blueprint=body.blueprint,
            repo=body.repo,
            trigger_type="dashboard",
            label=f"dashboard:{body.repo}"[:80],
            agent_context=agent_context,
            principal=principal.to_dict() if principal else None,
            trace_id=trace_id_from_request(request),
            autonomy=(body.autonomy or "").lower(),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"run_id": task_id, "stage": "triggered", "repo": body.repo}


@router.post("/runs/{task_id}/retrigger")
async def retrigger(request: Request, task_id: str) -> dict[str, Any]:
    """Re-dispatch a finished/failed run with its ORIGINAL intent + repo."""
    from devai.identity import trace_id_from_request

    svc = _service(request)
    original = await svc.get_task(task_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    ctx = original.get("agent_context") or {}
    intent = (original.get("intent") or ctx.get("requirements") or "").strip()
    repo = original.get("repo") or ""
    if not intent:
        raise HTTPException(status_code=400, detail="run has no original requirements to replay")
    # Retriggering re-runs the original work — enforce team membership on the
    # source run's owning team (CODE-2) and require a principal when auth is on.
    principal = await _authorize_run(request, original)
    new_id = await svc.dispatch(
        intent=intent,
        blueprint=original.get("blueprint"),
        repo=repo,
        trigger_type="dashboard-retry",
        label=f"retry:{task_id}"[:80],
        agent_context={"requirements": intent, "retry_of": task_id},
        principal=principal.to_dict() if principal else None,
        trace_id=trace_id_from_request(request),
    )
    return {"run_id": new_id, "stage": "triggered", "repo": repo, "retry_of": task_id}


async def _set_control(request: Request, task_id: str, value: str) -> dict[str, Any]:
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        # The Fleet list also shows legacy-orchestrator runs (devai:run:*) that
        # aren't pipeline tasks — fall back so pause/resume/stop work on them too
        # (previously this 404'd and the dashboard silently swallowed it).
        task = await svc.get_legacy_run(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    # Run-control (pause/resume/stop) is a privileged action — require a
    # principal (when auth is on) and enforce team membership (CODE-2).
    await _authorize_run(request, task)
    await svc.set_run_control(task_id, value)
    return {"run_id": task_id, "control": value}


@router.post("/runs/{task_id}/resume-failed")
async def resume_failed(request: Request, task_id: str) -> dict[str, Any]:
    """Continue a failed/cancelled run FROM WHERE IT LEFT OFF.

    Completed stages are skipped on replay; execution picks up at the failed
    stage with its full context (epic, stories, PR, branch, heal history).
    Use /retrigger instead for a clean full re-run as a new run.
    """
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    await _authorize_run(request, task)
    try:
        return await svc.resume_from_failure(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/runs/{task_id}/pause")
async def pause(request: Request, task_id: str) -> dict[str, Any]:
    """Pause a run at the next stage boundary (in-process executor)."""
    return await _set_control(request, task_id, "paused")


@router.post("/runs/{task_id}/resume")
async def resume(request: Request, task_id: str) -> dict[str, Any]:
    """Resume a paused run."""
    return await _set_control(request, task_id, "running")


@router.post("/runs/{task_id}/stop")
async def stop(request: Request, task_id: str) -> dict[str, Any]:
    """Stop a run — it cancels (not fails) at the next stage boundary."""
    return await _set_control(request, task_id, "stopped")


@router.delete("/runs/{task_id}")
async def delete_run(request: Request, task_id: str) -> dict[str, Any]:
    """Delete a run — stops it if live, then removes it from the pipeline-task
    store, the legacy orchestrator store, and clears its control flag. Lets the
    dashboard clean up old / zombie runs."""
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        task = await svc.get_legacy_run(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    # Deleting a run is privileged — same guard as run-control.
    await _authorize_run(request, task)
    await svc.delete_run(task_id)
    return {"run_id": task_id, "deleted": True}


@router.post("/runs/{task_id}/inject")
async def inject(request: Request, task_id: str) -> dict[str, Any]:
    """Inject an extra requirement/message into a running run.

    Recorded on the task's handover bag for a re-planning stage (Supervisor /
    crew lead) to pick up. Returns 404 if the run isn't currently in memory.
    """
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    await _authorize_run(request, task)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if not await svc.inject_message(task_id, message):
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not in memory")
    return {"status": "injected", "run_id": task_id}


_DEFAULT_PIPELINE_CONFIG: dict[str, Any] = {
    "auto_mode": False,
    "gates": {"deployment": True, "testing": True, "review": False, "merge": True, "createPR": False},
    "max_review_iterations": 3,
    "branch_template": "story/{story_number}-{description}",
}


def _config_redis(request: Request):
    sm = getattr(_service(request), "state_manager", None)
    return getattr(sm, "redis", None)


@router.get("/config")
async def get_config(request: Request, repo: str = "default") -> dict[str, Any]:
    """Per-repo pipeline config (gates / model settings). Engine-agnostic."""
    redis = _config_redis(request)
    if redis is not None:
        raw = await redis.get(f"devai:config:{repo}")
        if raw:
            return json.loads(raw)
    return dict(_DEFAULT_PIPELINE_CONFIG)


@router.post("/config")
async def save_config(request: Request) -> dict[str, str]:
    """Persist per-repo pipeline config."""
    await _require_principal(request)
    body = await request.json()
    repo = body.get("repo", "default")
    redis = _config_redis(request)
    if redis is not None:
        await redis.set(f"devai:config:{repo}", json.dumps(body))
        return {"status": "saved", "repo": repo}
    return {"status": "unavailable", "repo": repo}


# ────────────────────────────────────────────────────────────────────
# Approval gates (durable human-in-the-loop via Temporal Signals)
# ────────────────────────────────────────────────────────────────────


@router.get("/runs/{task_id}/approvals")
async def list_approvals(request: Request, task_id: str) -> list[dict[str, Any]]:
    """List a run's approval gates and their decision/pending status."""
    return await _service(request).list_gates(task_id)


@router.post("/runs/{task_id}/approvals/{gate}/approve")
async def approve(request: Request, task_id: str, gate: str) -> dict[str, str]:
    """Approve a gate — the durable workflow (or in-process gate) proceeds."""
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    # Human-in-the-loop guardrail: an approval must come from an authenticated
    # team member, and we record who approved (CODE-2).
    principal = await _authorize_run(request, task)
    await svc.approve_gate(task_id, gate, "approved", approver=principal.to_dict() if principal else None)
    return {"status": "approved", "gate": gate, "run_id": task_id}


@router.post("/runs/{task_id}/approvals/{gate}/reject")
async def reject(request: Request, task_id: str, gate: str) -> dict[str, str]:
    """Reject a gate — the run is cancelled at the gate."""
    svc = _service(request)
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run {task_id!r} not found")
    principal = await _authorize_run(request, task)
    await svc.approve_gate(task_id, gate, "rejected", approver=principal.to_dict() if principal else None)
    return {"status": "rejected", "gate": gate, "run_id": task_id}


# ────────────────────────────────────────────────────────────────────
# Checkpoint rollback
# ────────────────────────────────────────────────────────────────────


class RollbackBody(BaseModel):
    sha: str = Field(..., description="Checkpoint commit SHA to reset the working tree to")


@router.post("/runs/{task_id}/rollback")
async def rollback_run(request: Request, task_id: str, body: RollbackBody) -> dict[str, str]:
    """Roll a task's sandbox working tree back to a prior checkpoint.

    Validates the SHA against the task's recorded checkpoints, then asks the
    pipeline to re-dispatch a `git reset --hard` in the task's worktree. The
    actual reset runs in the runner pod (via the `rollback` tool); here we
    just record intent + return. Returns 404 if the SHA isn't a known
    checkpoint of this task.
    """
    svc = _service(request)
    snapshot = svc.get_task_in_memory(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="task not found")
    # Rolling a working tree back is a privileged, run-mutating action.
    await _authorize_run(request, snapshot)
    known = {cp.get("sha") for cp in (snapshot.get("checkpoints") or [])}
    if body.sha not in known:
        raise HTTPException(status_code=404, detail="sha is not a checkpoint of this task")
    requested = await svc.request_rollback(task_id, body.sha)
    return {"task_id": task_id, "sha": body.sha, "status": "requested" if requested else "unavailable"}


# ────────────────────────────────────────────────────────────────────
# SSE stream
# ────────────────────────────────────────────────────────────────────


@router.get("/events/stream")
async def stream_events(request: Request, replay: int = Query(0, ge=0, le=1000)):
    """Server-Sent Events feed of stage events.

    Each event is JSON-encoded under the `data:` field. Reconnecting
    clients can pass `?replay=200` to get the last 200 buffered events.
    """
    svc = _service(request)

    async def iterator():
        # Initial hello so the browser knows the stream is open.
        yield "event: hello\ndata: {}\n\n"
        try:
            async for ts, task_id, payload in svc.event_stream(replay=replay, heartbeat_seconds=15.0):
                if await request.is_disconnected():
                    return
                if payload is None:
                    # Heartbeat sentinel — a comment keeps ingress proxies
                    # (Istio/Envoy idle timeouts) from killing quiet streams.
                    yield ": keep-alive\n\n"
                    continue
                event_id = f"{ts:.6f}-{task_id}"
                data = json.dumps(
                    {"timestamp": ts, "task_id": task_id, **payload},
                    default=str,
                )
                # Typed envelope: the wire event name is the envelope type
                # (stage | agent_status | a2a). Existing listeners bound to
                # "stage" keep working; new consumers subscribe per type.
                event_name = payload.get("event_type") or "stage"
                yield f"id: {event_id}\nevent: {event_name}\ndata: {data}\n\n"
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
