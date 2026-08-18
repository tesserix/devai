"""Turn-level agent observability.

LLM providers run their tool loops deep inside agents that know nothing
about runs or dashboards. This module bridges that gap with two pieces
of ambient state:

  - a **contextvar turn context** ``{run_id, agent, stage}`` — set by the
    blueprint executor around each stage execution, inherited by every
    coroutine the stage spawns (including the provider's loop);
  - a **process-global sink** — registered once by the PipelineService;
    it fans each envelope out to the SSE ring, live subscribers, and the
    durable per-run Redis log.

Providers call :func:`emit_turn` fire-and-forget; with no context or no
sink it's a no-op, so unit tests and CLI paths are unaffected.

Envelope kinds (all carry ``event_type: "agent_turn"``):

  agent_start  — model, max turns/sessions
  turn         — turn number, session, usage in/out tokens, the model's
                 narration text, and the tool calls it made
  tool_result  — only for FAILED tools (errors must be visible)
  checkpoint   — session boundary note (completed / remaining)
  agent_done   — reason (natural | remaining_none | budget_exhausted),
                 totals
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# {run_id, agent, stage} — set per stage execution by the executor.
_turn_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("devai_turn_ctx", default=None)

# async def sink(run_id: str, envelope: dict) -> None
_sink: Callable[..., Any] | None = None


def set_turn_context(run_id: str, agent: str, stage: str) -> contextvars.Token:
    """Install the turn context for the current task. Returns the token so
    the caller can restore the previous context when the stage finishes."""
    return _turn_ctx.set({"run_id": run_id, "agent": agent or "", "stage": stage or ""})


def update_turn_context(**fields: str) -> None:
    """Merge extra fields into the CURRENT turn context (no-op without one).

    Used by AgentAdapter to attach billing attribution — ``triggered_by``
    (who the spend belongs to) and ``trial`` (meter this usage against
    their free-trial budget) — so every envelope a legacy provider emits
    carries enough for the sink to record usage per user."""
    ctx = _turn_ctx.get()
    if ctx is None:
        return
    _turn_ctx.set({**ctx, **{k: v for k, v in fields.items() if v}})


def get_turn_context() -> dict[str, str]:
    """A copy of the current run/identity attribution context."""
    return dict(_turn_ctx.get() or {})


def reset_turn_context(token: contextvars.Token) -> None:
    _turn_ctx.reset(token)


def set_turn_sink(sink: Callable[..., Any] | None) -> None:
    """Register the process-global envelope sink (PipelineService)."""
    global _sink
    _sink = sink


async def emit_turn(kind: str, **fields: Any) -> None:
    """Emit one turn envelope. No context or sink → silent no-op.

    Never raises — observability must not break the agent loop.
    """
    ctx = _turn_ctx.get()
    if ctx is None or _sink is None:
        return
    envelope = {
        "event_type": "agent_turn",
        "kind": kind,
        "agent": ctx["agent"],
        "stage": ctx["stage"],
        "timestamp": time.time(),
        **fields,
    }
    # Billing attribution riding the context (update_turn_context): who the
    # spend belongs to, and whether it draws down their trial budget.
    for key in ("triggered_by", "tenant_id", "user_id", "trial"):
        if ctx.get(key):
            envelope.setdefault(key, ctx[key])
    try:
        await _sink(ctx["run_id"], envelope)
    except Exception:  # noqa: BLE001
        logger.debug("turn sink failed (kind=%s)", kind, exc_info=True)


__all__ = [
    "emit_turn",
    "get_turn_context",
    "reset_turn_context",
    "set_turn_context",
    "set_turn_sink",
    "update_turn_context",
]
