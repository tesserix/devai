# Real-Time Run Observability — Implementation Plan

Goal: while a blueprint run executes, the Fleet/run pages show **live** truth —
each stage's related agent lights up as it works, the supervisor/orchestrator
coordination layer reflects actual progress, A2A messages stream in as they
happen, the Events tab updates in real time — and every one of those signals
is **durably logged** (survives pod restarts) with NATS JetStream carrying the
canonical event flow.

---

## 1. Current state (audited 2026-06-11, file:line verified)

### What already works

| Piece | Where | Status |
|---|---|---|
| Executor emits one `StageEvent` per (stage, phase) | `blueprint/executor.py:307` → `PipelineService._on_event` (`pipeline/service.py:734`) | ✓ |
| Stage events fan out: in-proc ring (1000) + SSE queues + Redis task snapshot + NATS publish | `service.py:756-814` | ✓ |
| SSE endpoint with replay + Last-Event-ID | `GET /api/pipeline/events/stream` (`pipeline/routes.py:517`) | ✓ (used only by run-detail Logs + Compose) |
| NATS JetStream stream `DEVAI`, subjects `devai.>`, FILE storage, 7d | `adapters/event_bus/nats_adapter.py:100-157` | ✓ publish side |
| `stage_events` persisted on the task record (Redis, TTL) | `pipeline/types.py:367`, `core/state.py:191` | ✓ |
| Legacy LangGraph path: A2A bus + `set_agent_status` per agent | `core/base_agent.py:130-261`, `core/state.py:97-115` | ✓ but only legacy |
| Dashboard tabs (Overview/Hierarchy/Agents/A2A/Events) | `dashboard/src/app/page.tsx` | ✓ render, 3s poll |

### The gaps (why everything shows "Idle")

| # | Gap | Evidence |
|---|---|---|
| G1 | **Blueprint runs never produce an `agents` status dict.** `DevAITask.to_dict()` has no `agents`; `set_agent_status` is only called in `core/base_agent.py:261` (legacy). The dashboard's `run.agents` is `{}` → every card renders the "Idle" fallback. | `pipeline/types.py:355-375`, `agent-card.tsx:82` |
| G2 | **Blueprint runs never emit A2A messages.** No `A2ABus` in `StageDeps`; no stage writes `agent_context["a2a_messages"]`; `/api/pipeline/runs/{id}/a2a` returns `[]`. | `pipeline/interfaces.py`, `pipeline/routes.py:141` |
| G3 | **`StageEvent` carries no agent identity** — consumers can't map a stage event to the agent card that should light up. (`StageSpec.agent` + `resolved_agent()` exist at the executor; the event just doesn't copy them.) | `pipeline/types.py:129-165`, `blueprint/loader.py:99,118` |
| G4 | **Fleet home is polling-only** (runs 5s, detail 3s). The SSE stream exists but isn't consumed there; agent/A2A/event updates lag and feel static. | `app/page.tsx:43-63` |
| G5 | **Supervisor/orchestrator "coordination layer" has no runtime signal** — `orchestrator_routing` (progress_pct/current_phase) is only written by the legacy path. | `agent-hierarchy.tsx:35-47` |
| G6 | **Event history is not durable.** The ring is in-memory (`service.py:97`); `stage_events` on the task survive, but A2A and agent-status transitions have no persisted log for blueprint runs at all. | |
| G7 | **JetStream retention is `WORK_QUEUE`** — wrong for observability fan-out: work-queue semantics restrict overlapping consumers and assume each message is consumed exactly once; with no consumer attached the stream just accretes for 7d. | `nats_adapter.py:113-156` |
| G8 | Run-detail A2A poll (6s) vs Fleet (3s) inconsistency; no reconciliation strategy documented. | `runs/[id]/page.tsx:142` |

---

## 2. Target architecture — one run-event spine

```
blueprint executor ──► StageEvent{stage, phase, agent ←NEW}
                            │
                  PipelineService._on_event   (the hub — already exists)
                            │
        ┌─────────┬─────────┼──────────────┬───────────────┐
        ▼         ▼         ▼              ▼               ▼
   ring buffer  SSE queues  Redis task    NATS JetStream   NEW derivations
   (replay)    (live push)  snapshot      devai.pipeline.*    │
                                                              ├─ agents dict on the task
                                                              │  (running/completed/failed per agent
                                                              │   + supervisor/orchestrator synthesis)
                                                              ├─ synthetic A2A coordination messages
                                                              │  (handoff/response/escalation), appended to
                                                              │  agent_context + Redis devai:run:{id}:a2a_messages
                                                              └─ orchestrator_routing (progress_pct,
                                                                 current_phase, status_summary)
```

**Design principle:** the executor's stage events are the single source of
truth; everything the UI needs (agent status, coordination A2A, progress) is
*derived once, server-side, in the hub* — not re-implemented per stage, not
guessed client-side. Real A2A from legacy agents running inside
`AgentAdapter` stages merges into the same streams.

SSE becomes a **typed envelope** — `{type: "stage"|"agent_status"|"a2a"|"task",
task_id, data}` — one connection per dashboard page drives every tab.
Polling stays as the reconciliation fallback (SSE connected → poll slows to
15s; disconnected → 3s as today).

---

## 3. Phases

### R1 — Backend: emit the missing signals (the core chunk)

| # | Change | Files |
|---|---|---|
| R1.1 | `StageEvent.agent` field; executor copies `spec.resolved_agent()` (and `lane`) into every event; `to_dict`/`from_dict` roundtrip | `pipeline/types.py`, `blueprint/executor.py` |
| R1.2 | **Agent-status derivation in the hub**: on STARTED → `agents[agent] = running`; COMPLETED → `completed` (+duration); FAILED → `failed` (+error); SKIPPED → `skipped`. Stored on the task (`task.agents`, serialized in `to_dict`) so `/api/pipeline/runs/{id}` feeds the cards with zero new endpoints; mirrored to Redis `devai:run:{id}:agents` (existing `set_agent_status`) for parity with legacy readers | `pipeline/service.py`, `pipeline/types.py`, `core/state.py` (reuse) |
| R1.3 | **Coordination synthesis**: task RUNNING → `supervisor`+`orchestrator` = running; terminal → completed/failed. `orchestrator_routing` = {progress_pct: completed/total from the blueprint graph, current_phase: active stage's lane, status_summary} maintained in `agent_context` on every event | `pipeline/service.py` |
| R1.4 | **Synthetic A2A coordination messages**: STARTED → `handoff` supervisor→agent ("Stage X handed to Y"); COMPLETED → `response` agent→supervisor (duration, message); FAILED → `escalation` agent→supervisor (error). Appended to `agent_context["a2a_messages"]` (cap 500) AND `rpush`ed to `devai:run:{id}:a2a_messages` (TTL = pipeline_task_ttl) — same key the legacy endpoint already serves | `pipeline/service.py` |
| R1.5 | **Real A2A from legacy agents inside blueprint stages**: `AgentAdapter._extract_outputs` keeps `a2a_messages` from the agent patch and merges them (dedup by id) into `agent_context["a2a_messages"]` + Redis | `pipeline/stages/_base.py` |
| R1.6 | **Durable event log**: hub `rpush`es every typed envelope to `devai:run:{id}:events` (cap 2000, TTL) — the "logged as well" requirement; `/api/pipeline/events/recent` falls back to it after restart | `pipeline/service.py`, `pipeline/routes.py` |
| R1.7 | Tests: agent-status mapping (all phases), supervisor synthesis, a2a synth content + caps + Redis writes, A2A merge from AgentAdapter patches, restart-survival of the event log | `tests/unit/` |

### R2 — Backend: typed SSE + JetStream hygiene

| # | Change | Files |
|---|---|---|
| R2.1 | SSE envelope: `_on_event` already pushes stage events; also push `agent_status` and `a2a` envelopes to the same queues; event name on the wire = envelope type (back-compat: existing `stage` listeners unaffected) | `pipeline/service.py`, `pipeline/routes.py` |
| R2.2 | NATS: publish the same typed envelopes (`devai.pipeline.a2a.{task_id}`, `devai.pipeline.agent.{agent}.{status}`) alongside existing subjects — JetStream remains the canonical durable bus | `pipeline/service.py` |
| R2.3 | JetStream stream config: retention `LIMITS` (fan-out friendly) instead of `WORK_QUEUE`, keep FILE + 7d max age + max_msgs cap; config-driven (`DEVAI_NATS_STREAM_RETENTION`) so existing deployments migrate by env var. Stream update handles the retention change (delete+recreate is NOT acceptable: log + keep old retention if update fails) | `adapters/event_bus/nats_adapter.py`, `config.py` |
| R2.4 | SSE robustness: heartbeat comment every 15s (proxy keep-alive), bounded queue back-pressure (drop-oldest per slow client), client count telemetry (`devai.sse.clients`) | `pipeline/service.py`, `pipeline/routes.py` |

### R3 — Dashboard: live wiring

| # | Change | Files |
|---|---|---|
| R3.1 | `useRunEvents(taskId)` hook: one `EventSource` on `/api/pipeline/events/stream?replay=200`, filters by task_id, dispatches typed envelopes; exposes `connected` flag | `dashboard/src/lib/use-run-events.ts` (new) |
| R3.2 | Fleet home (`app/page.tsx`): hook drives — agent cards (status transitions animate immediately), A2A feed (append live), Events tab (prepend live), DAG overlay (stage phase flips), hierarchy progress. Poll drops to 15s reconciliation while `connected`, 3s otherwise. "● LIVE" indicator chip when connected | `app/page.tsx` |
| R3.3 | Run-detail page: same hook for its Timeline/A2A/DAG | `app/runs/[id]/page.tsx` |
| R3.4 | A2A feed: autoscroll-on-new (unless user scrolled up), message-type colors already exist | `components/a2a-feed.tsx` |

### R4 — Prod-readiness mechanisms

- **Restart resilience**: replay from ring → falls back to `devai:run:{id}:events` Redis log (R1.6); dashboard reconnect uses `Last-Event-ID` (already implemented server-side).
- **Multi-replica readiness**: devai-api is deliberately pinned to 1 replica
  (KEDA, documented in values-prod) because SSE + ring live in-process. For
  the future multi-replica case: optional NATS fan-in (`DEVAI_EVENT_FANIN_ENABLED`)
  — each pod subscribes `devai.pipeline.>` and re-feeds its local hub. Designed
  now (envelopes already on NATS via R2.2), implemented behind the flag only.
- **Caps/TTLs everywhere**: a2a 500/run, events 2000/run, both TTL'd by
  `pipeline_task_ttl`; ring stays 1000; SSE queues bounded (drop-oldest).
- **Telemetry**: `devai.run_events` counter {type}, `devai.sse.clients` gauge,
  hub derivation errors counter. All through the existing telemetry adapter.
- **Failure isolation**: every sink in the hub is individually try/excepted —
  a Redis hiccup cannot break SSE; NATS down cannot break the run (already the
  pattern; extended to new sinks).
- **Auth**: SSE + a2a endpoints stay behind the existing session middleware.

### R5 — Verification

- Unit: hub derivations, envelope shapes, caps, AgentAdapter a2a merge,
  JetStream config builder.
- Integration (local): `kind` sandbox run of `app-scaffold`/`alm-pipeline`
  blueprint → assert agents dict transitions, a2a list non-empty, Redis event
  log populated, SSE stream delivers all three envelope types.
- Prod validation after rollout: trigger a run; watch Fleet page tabs go
  live; `kubectl exec` read-only checks on `devai:run:*:a2a_messages` and
  `:events` keys; NATS stream info shows LIMITS retention.

---

## 4. Explicit non-goals (this iteration)

- WebSocket transport (SSE + replay covers the need; one fewer surface).
- Rewriting legacy LangGraph A2A — it keeps working; blueprint runs reach parity.
- Cross-run global activity feed (per-run scope only; the global feed reuses
  the same envelopes later).
- kagent/K8s-Job agent runtimes — Job-based stages already report via the
  executor's stage events, which is the spine everything hangs from.
