# NATS JetStream WorkQueue — run dispatch backbone

**Status:** Planned → building (Phase 1) — 2026-06-17.
**Goal:** replace the hand-rolled Redis *claim guard* on the shared pipeline-run
queue with a native JetStream **WorkQueue**, so run dispatch gets single-delivery,
redelivery, dead-lettering, lane routing, and KEDA autoscaling for free — and the
api↔sre release ping-pong disappears.

---

## Why

Today `pipeline.py` runs a durable queue in Redis (`core/state.py`): workers
`claim_next_task(worker, claim_ttl)` (BLMOVE), `_heartbeat` refreshes the claim,
`ack_task` clears it, a `reaper` requeues expired claims, and — because **api and
sre share one Redis queue but load different blueprints** — a worker that can't run
a blueprint `release_task`s it back (ping-pong, capped at 50 releases → fail). It
works, but it re-implements, by hand, exactly what a JetStream WorkQueue does
natively — and the ping-pong is wasted work.

| Hand-rolled today (Redis) | JetStream WorkQueue (native) |
|---|---|
| `claim_next_task` (BLMOVE, exactly-once) | queue-group consumer → **one** worker per msg |
| `_heartbeat` refresh every `claim_ttl/4` | `msg.in_progress()` extends `ack_wait` |
| `ack_task` on exit | `msg.ack()` |
| `reaper` requeues expired claims | `ack_wait` elapses → automatic redelivery |
| `release_task` ping-pong (wrong service) | **lane subjects** — each service only gets its own |
| (no dead-letter — `releases>50` hard-fail) | `max_deliver` → **DLQ** subject + run marked failed |
| poison run loops until manual stop | `term()` / DLQ kills it deterministically |

## Architecture

```
producer (PipelineService.dispatch)
  └─ persist snapshot (Redis/Postgres = system of record)   ← UNCHANGED
  └─ dispatch.enqueue(task)
       publish  devai.pipeline.run.<lane>   headers: Nats-Msg-Id=<task.id>   ← dedup
                                            payload: {"task_id": "..."}        ← id only

stream  DEVAI_PIPELINE_RUNS  (WorkQueue retention, subjects devai.pipeline.run.*)

worker pool  (queue group "devai-pipeline-workers", durable per lane)
  on message:
    task_id ← msg.json()
    if run already terminal           → msg.ack();  return            (idempotent)
    snapshot ← state.load; rebuild DevAITask
    if control == "stopped"           → finalize cancel; msg.ack()     (stop-guard kept)
    start in_progress heartbeat (every ack_wait/2)
    execute run (BlueprintExecutor — skips completed stages, so redelivery resumes)
    success / terminal                → msg.ack()
    transient fail / shutdown:
        if msg.num_delivered >= max_deliver → publish DLQ; mark run FAILED; msg.term()
        else                                → msg.nack()  (redeliver after ack_wait)

KEDA ScaledObject → scales the worker Deployment on the consumer's num_pending (0→N)
DLQ consumer       → devai.pipeline.run.dlq → mark run failed + emit event + alert
```

**Lane routing kills the ping-pong.** Each service publishes to AND subscribes on
its own lane (`DEVAI_PIPELINE_DISPATCH_LANE`: api=`alm`, sre=`sre`). A service only
dispatches blueprints it loaded, so a run never lands on a service that can't run it
— no `release_task`, no 50-release cap. (A stray cross-lane msg still nacks→DLQs, so
it degrades safely.)

**Persistence is unchanged.** The queue carries only the `task_id`; the snapshot
stays in Redis/Postgres (system of record). So this swaps the *transport + claim
semantics*, not the durable run state — the executor's resume-from-snapshot,
stop-guard, and event fan-out are all reused verbatim.

## The DispatchBackend seam (additive, safe)

`pipeline.py` selects a backend by `DEVAI_PIPELINE_DISPATCH_BACKEND`:

- `redis` (**default**) — the existing `_durable_worker_loop` (battle-tested, untouched).
- `nats` — the new `_nats_worker_loop` over `NatsWorkQueueBackend`.
- `inproc` — the existing in-memory `_worker_loop` (tests/CLI).

So this ships **off by default**: flip one env var per service to adopt it, flip
back to roll back. No behavior change until opted in.

## Adapter extensions (small, in the family)

`adapters/event_bus` gains two things the WorkQueue needs, both additive:

1. `EventMessage.in_progress()` + `_in_progress` callback — extend the ack deadline
   for long runs (the JetStream heartbeat). Noop backends no-op it.
2. `EventMessage.num_delivered: int` — bound from `msg.metadata.num_delivered`, so the
   worker can dead-letter in-band on the final attempt.

`NatsEventBusAdapter._wrap_handler` binds both. Nothing else changes; `noop` keeps
satisfying the ABC.

## Config (config.py)

```
pipeline_dispatch_backend: str = "redis"          # redis | nats | inproc
pipeline_dispatch_lane: str = "alm"               # this service's lane (sre sets "sre")
pipeline_runs_stream: str = "DEVAI_PIPELINE_RUNS"
pipeline_run_subject_prefix: str = "devai.pipeline.run"
pipeline_dispatch_queue_group: str = "devai-pipeline-workers"
# ack_wait + max_deliver reuse the existing nats_ack_wait (300) / nats_max_deliver (3).
```

## Dead-letter handling

- In-band: the worker checks `num_delivered`; on the **last** allowed attempt it
  publishes the run to `devai.pipeline.run.dlq` and `term()`s (no further redelivery).
- A DLQ consumer (one per service, or a small dedicated reaper) marks the run
  `STAGE_FAILED` with `error="dead-lettered after N attempts"`, emits a run event, and
  logs/alerts. This is strictly better than today (poison runs currently loop or
  hard-fail without a record).

## Robustness checklist

- **Idempotent publish** — `Nats-Msg-Id = task.id` → JetStream dedups within the
  stream's dedup window (re-dispatch / reconcile can't double-enqueue).
- **Idempotent consume** — terminal-run check acks-and-returns; the executor skips
  completed stages, so a redelivery resumes rather than re-runs.
- **Long runs** — `in_progress()` heartbeat every `ack_wait/2` (mirrors the Redis
  `claim_ttl/4`); a crashed worker stops heartbeating → redelivery after `ack_wait`.
- **Graceful shutdown** — on SIGTERM, in-flight msg is `nack`ed (immediate redeliver
  to a surviving worker) — the JetStream analog of `handoff_task`.
- **Degradation** — `create_event_bus_adapter` returns Noop if NATS/SDK is missing;
  the backend selector falls back to `redis` if the adapter can't connect, so a NATS
  outage never strands dispatch.
- **No SPOF** — the system of record stays Redis/Postgres; NATS is the transport.

## KEDA autoscaling (tesserix-k8s) — Phase 1.5

**Prerequisite — producer/worker split.** Today `devai-api` *is* the worker (it
runs `PipelineService` with in-pod workers) and is pinned to 1 replica. To
autoscale on backlog you split roles:

- `devai-api` → **producer + hub**: `dispatch()` publishes to the lane; serves the
  SSE/event hub. Stays at 1 (or a small fixed count). Set `pipeline_dispatch_backend=nats`
  but run **0 in-pod workers** (a `pipeline_worker_replicas`-style flag, or just don't
  call `pipeline.start()`'s subscribe on the api).
- `devai-pipeline-worker` → **consumer**: a new Deployment running the same image
  with `DEVAI_PIPELINE_DISPATCH_BACKEND=nats` + the api's worker role, **0 producers**.
  This is what KEDA scales 0→N on the JetStream consumer backlog.

Both share the same lane subject + queue group, so the producer's publishes fan out
to the worker pool. The api keeps its current Redis path until the worker Deployment
exists — flip the api to `nats` only once workers are consuming.

**ScaledObject** (NATS JetStream scaler on the consumer's pending count):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: devai-pipeline-worker
  namespace: devai
spec:
  scaleTargetRef:
    name: devai-pipeline-worker        # the consumer Deployment
  minReplicaCount: 0                    # scale to zero when idle (cost)
  maxReplicaCount: 10
  cooldownPeriod: 120
  pollingInterval: 10
  triggers:
    - type: nats-jetstream
      metadata:
        natsServerMonitoringEndpoint: "nats.nats.svc.cluster.local:8222"
        account: "$G"
        stream: "DEVAI_PIPELINE_RUNS"
        consumer: "pipeline-alm"        # durable name = pipeline-<lane>
        lagThreshold: "2"               # ~2 pending runs per replica
        activationLagThreshold: "1"     # wake from 0 on the first run
```

Notes: the JetStream scaler reads `num_pending` off the NATS monitoring port
(`8222`); `lagThreshold` is pending-msgs-per-replica. Scale-to-zero pairs naturally
with the WorkQueue — no idle workers, KEDA spins them on the first queued run.

## Status (this change)

Phase 1 **built + tested, off by default** (`pipeline_dispatch_backend=redis`):
adapter `in_progress()`/`num_delivered`, `NatsWorkQueueBackend`, `_handle_nats_run`,
config, DLQ, and `tests/unit/test_dispatch_workqueue.py` (all green). Flip
`DEVAI_PIPELINE_DISPATCH_BACKEND=nats` on a canary to adopt; Phase 1.5 adds the
worker Deployment + ScaledObject above.

## Phasing

- **Phase 1 (this change):** adapter extensions, `NatsWorkQueueBackend`,
  `_nats_worker_loop`, config, DLQ, contract tests. Ships `backend=redis` (off).
- **Phase 1.5:** KEDA ScaledObject + values; enable `backend=nats` on a canary.
- **Phase 2 (separate plan):** per-**stage** agent-task WorkQueue (`devai.agent.task.<agent>`)
  so `JobRunnerStage` publishes a task and a warm worker pool runs the agent —
  decouples agent execution from the orchestrator. The run-level queue here is the
  foundation it builds on.

## Tests

`tests/unit/test_dispatch_workqueue.py` — against a fake event bus + noop:
enqueue publishes with `Nats-Msg-Id`; consume on terminal run acks-without-executing;
transient failure nacks; final attempt DLQs+terms+marks failed; `in_progress` called
during a long run; shutdown nacks in-flight. Plus an `EventMessage` unit test for the
new `in_progress`/`num_delivered` plumbing.
