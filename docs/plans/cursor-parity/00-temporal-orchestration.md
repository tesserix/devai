# 00 — Temporal: the Durable Execution Backbone

**Priority: P0 — foundational. Everything else (01–11) runs on top of this.**

> ## ✅ Implemented — generic backbone (Phase 1)
>
> The durable-execution seam is now in code, and it is **blueprint- and
> agent-agnostic by construction**. The one deviation from the original sketch
> below: instead of a hand-written workflow *per pipeline* (`alm_pipeline.py`,
> `pr_review.py`, …), there is **one generic `BlueprintWorkflow`** that interprets
> *any* blueprint DAG and runs each stage through **one generic `run_stage`
> activity**. A new blueprint (simple or complex) or a new agent runs durably with
> **zero code changes** — you only add YAML. This is simpler (1 workflow, not N)
> and is the only shape that keeps the "any blueprint, no code" invariant.
>
> **What shipped:**
> - `src/devai/adapters/workflow/{base,inproc,temporal,noop,factory}.py` — the
>   adapter family. `DEVAI_WORKFLOW_PROVIDER=inproc|temporal|noop` (default
>   `inproc` = today's behaviour, byte-for-byte). Factory degrades to in-proc if
>   `temporalio` is absent; the temporal adapter degrades per-run if the cluster
>   is unreachable.
> - `src/devai/orchestration/{workflows,activities,serde,context,worker}.py` —
>   the generic `BlueprintWorkflow` + `run_stage` activity + the
>   `python -m devai.orchestration.worker` entrypoint.
> - `src/devai/blueprint/planner.py` — pure ordering logic shared verbatim by the
>   in-process executor (`_topo_sort` delegates to it) and the Temporal workflow.
> - `src/devai/pipeline/bootstrap.py` — single `build_runtime()` so the service
>   and the worker wire StageDeps/registry identically.
> - `Pipeline` routes execution through the workflow adapter; `config.py` carries
>   the `workflow_provider` + `temporal_*` block; `tests/unit/test_workflow_adapter.py`
>   (20 tests).
>
> **Still to do (later phases):** add `temporalio` to deps (currently optional;
> degrades without it), the Temporal server + `devai-worker` Helm charts in
> `tesserix-k8s`, encrypted payload codec, per-queue workers, approval-gate
> Signals, Schedules for SRE, child-workflow fan-out. The sketch below is the
> reference for those.

This is the layer that makes DevAI **production‑ready**: agent runs survive pod
restarts, retries/timeouts are declarative (not hand‑rolled), human approvals are
durable waits (not in‑memory state), schedules and long‑running agents are
first‑class, and side effects are exactly‑once.

## Why Temporal (the problem it solves)

Today DevAI orchestrates with LangGraph in‑process. That's fine for a single happy
path, but a control plane that runs unattended across many repos needs:

- **Crash safety.** If `devai-api`/a worker dies mid‑pipeline, work resumes
  *exactly where it left off* — every step is checkpointed. No "re‑run from
  scratch", no half‑applied side effects.
- **Durable retries & timeouts.** LLM rate‑limits, flaky tools, slow Jobs → retry
  policies + `schedule_to_close` / `start_to_close` / `heartbeat` timeouts, declared
  per Activity. We delete most of `services/resilience.py`'s hand‑rolled retry code.
- **Durable human‑in‑the‑loop.** Approval gates (plan 04) become a Workflow that
  *waits on a Signal* for days if needed — no polling, no lost state.
- **Long‑running / wake‑condition agents** (plan 10): durable timers +
  `continue_as_new` for unbounded loops without growing history.
- **Schedules** (SRE cron, automations) as a managed primitive, not a k8s CronJob.
- **Observability**: full event history per run in the Temporal UI, on top of
  LangSmith traces.

## The architectural split (keep LangGraph)

Temporal does **not** replace LangGraph — they layer:

```
Temporal Workflow  (deterministic orchestration: order, retries, gates, schedules)
        │  invokes
        ▼
Temporal Activity  (all side effects — non‑deterministic, retried)
        │  may run …
        ├── a LangGraph agent reasoning loop (the existing agents/)
        ├── an LLM call via adapters/llm
        ├── a tool call (gated by the safety classifier, plan 07)
        └── a K8s Job launch + await (background agents, plan 02)
```

Rule of thumb: **Workflow code is deterministic and side‑effect‑free** (no
network, no `time.now()`, no randomness — Temporal replays it). **Everything that
touches the outside world is an Activity.** LangGraph runs *inside* an Activity,
so the agent's own reasoning loop is unchanged; Temporal wraps it with durability.

## Adapter family (per CLAUDE.md)

Orchestration goes through a new family so it's swappable/testable:

```
src/devai/adapters/workflow/
  base.py        WorkflowAdapter ABC: start(), signal(), query(), schedule(), result()
  factory.py     create_workflow_adapter(settings)  # DEVAI_WORKFLOW_PROVIDER
  temporal.py    Temporal client (lazy `temporalio` import)
  inproc.py      runs workflows in-process (today's LangGraph-direct path)
  noop.py        MANDATORY fallback
```

`inproc` keeps local dev and unit tests free of a Temporal server; `temporal` is
prod. Factory never raises — missing server → degrade to `inproc`/`noop`.

## Where the workflows live

```
src/devai/orchestration/
  workflows/
    alm_pipeline.py        # the 14-node ALM pipeline as a Workflow
    sre_monitor.py         # SRE pipeline (run on a Temporal Schedule)
    background_task.py      # one background agent run (plan 02)
    pr_review.py            # ReviewBot + autofix loop (plan 03)
    automation.py           # a scheduled/condition automation (plan 10)
  activities/
    llm.py  tools.py  scm.py  k8s_job.py  index.py  memory.py  notify.py
  worker.py                # registers workflows+activities, polls task queues
```

## Capability → Temporal primitive (how 01–11 plug in)

| Plan | Temporal mechanism |
|---|---|
| 01 Indexing | Activities (`embed`, `upsert`); push‑sync as a short Workflow; full re‑index as a child workflow with heartbeating |
| 02 Background agents | one **Workflow per task**; `launch_job` + `await_job` Activities (heartbeat the Job's progress); worktree lifecycle in Activities |
| 03 ReviewBot/Autofix | review Workflow; autofix = **child workflow** per finding; bounded retry loop; re‑review via Signal on new commit |
| 04 Plan Mode | Workflow **waits on an approval Signal** (durable, days‑long); plan edits via Update |
| 05 Memories | `learn`/`recall` Activities; auto‑capture as a post‑run Activity |
| 06 Rules | pure Activity (load+assemble) — cached |
| 07 Safety classifier | `classify` Activity before each tool Activity; **escalate = wait on Signal**; sandbox = locked‑down Job Activity |
| 08 MCP | MCP tool calls are Activities (retry + timeout + audit) |
| 09 Parallel agents | **child workflows** fanned out (one per sub‑task), each its own worktree; parent awaits all → merge Activity |
| 10 Automations / loop | **Temporal Schedules** (cron) + Signals (wake conditions); long loops use `continue_as_new`; budget = workflow guard |
| 11 Ticketing | mention → `start_workflow(background_task)`; write‑back Activity links the PR |

## Streaming to the dashboards (2026 primitive)

Use **Workflow Streams** (Signals/Updates carrying token batches + app‑level
events) to drive the dashboard run/agent/a2a feeds and the chat SSE/WebSocket —
durable, replayable live updates instead of best‑effort in‑memory SSE. `chat/`
and the `a2a-feed` become consumers of workflow Queries/streams.

## Production hardening (the "prod‑ready" checklist)

- **Determinism**: workflows do no I/O, no clocks, no RNG; use
  `workflow.now()` / `workflow.uuid4()`. Lint with the Temporal sandbox.
- **Versioning**: `workflow.patched()` / worker Build IDs for safe rollout of
  changed workflow code without breaking in‑flight runs.
- **Timeouts/retries** per Activity: `start_to_close` always set;
  `heartbeat_timeout` for long Activities (Job await, full re‑index); explicit
  `RetryPolicy` (max attempts, backoff, non‑retryable error types).
- **Idempotency**: Activities that create PRs/commits/tickets take an idempotency
  key (workflow ID + attempt) so retries don't double‑post.
- **Task queues** per concern (`alm`, `sre`, `review`, `index`, `background`) so
  noisy workloads don't starve others; scale workers independently.
- **Multi‑tenancy**: Temporal **Namespace** per tenant/env (`devai`, `devai-sre`);
  isolates history + retention.
- **Security**: mTLS to the Temporal frontend; a **payload data converter** that
  encrypts inputs/outputs at rest (prompts/code never stored in cleartext in
  Temporal history) — pull the key from the `secrets` adapter.
- **Observability**: Temporal UI for history + metrics → Prometheus/OTel; keep
  LangSmith `@traceable` inside Activities; emit run IDs to `pipeline_runs`.
- **Retention/archival**: configure history retention + S3/GCS archival
  (`object_store` adapter) for audit.
- **Self‑hosted vs Cloud**: start self‑hosted on GKE (Helm) wired to the existing
  Postgres; Temporal Cloud is a later swap (adapter makes it a config change).

## Deployment (K8s / tesserix‑k8s)

- **Temporal server** (frontend/history/matching/worker + UI) via the official
  Helm chart, backed by our Postgres + Elasticsearch (visibility) — both already
  in `local-infra` for local. Prod chart under `tesserix-k8s/charts/apps/temporal`.
- **`devai-worker`** Deployment (new image / reuse `devai` image with
  `python -m devai.orchestration.worker`) — horizontally scalable, one per task
  queue group. ArgoCD app like the others.
- Local: add a `temporal` target to `sandboxctl` + `values-local.yaml`; the worker
  is just another chart.

## Implementation plan (phased)

- **Phase 1 — adapter + worker skeleton.** `adapters/workflow` (temporal+inproc+noop),
  `orchestration/worker.py`, one trivial workflow end‑to‑end; local Temporal via
  `local-infra`.
- **Phase 2 — port ALM pipeline.** Wrap the 14 nodes as a Workflow + Activities
  (LangGraph agents run inside Activities). Approval gate → Signal (folds in plan 04).
- **Phase 3 — SRE on a Schedule** (replaces the cron) + background‑task Workflow
  (plan 02) with Job launch/await Activities.
- **Phase 4 — review + parallel** as child workflows (plans 03, 09); safety
  classifier as gating Activity (plan 07).
- **Phase 5 — schedules/automations** (plan 10) + ticketing trigger (plan 11) +
  Workflow Streams to dashboards.
- **Phase 6 — prod hardening**: versioning, encrypted data converter, per‑queue
  workers, namespaces, retention/archival, dashboards/alerts.

## Files & modules

```
src/devai/adapters/workflow/{base,factory,temporal,inproc,noop}.py
src/devai/orchestration/{worker.py, workflows/*, activities/*}
src/devai/config.py                      # DEVAI_WORKFLOW_* block
tesserix-k8s/charts/apps/temporal/*      # server + UI
tesserix-k8s/charts/apps/devai-worker/*  # worker deployment
tests/unit/test_workflow_adapter.py
tests/integration/test_alm_workflow.py   # Temporal test env (time-skipping)
```

## Config (`DEVAI_*`)

```
DEVAI_WORKFLOW_PROVIDER=temporal          # temporal|inproc|noop
DEVAI_TEMPORAL_HOST=temporal-frontend.temporal.svc.cluster.local:7233
DEVAI_TEMPORAL_NAMESPACE=devai
DEVAI_TEMPORAL_TASK_QUEUE=alm
DEVAI_TEMPORAL_TLS_ENABLED=true
DEVAI_TEMPORAL_ENCRYPTION_KEY_SECRET=devai-temporal-codec   # data converter key
DEVAI_TEMPORAL_MAX_CONCURRENT_ACTIVITIES=50
```

## Acceptance criteria

- Kill `devai-worker` mid‑ALM‑run → a replacement worker resumes the run with no
  duplicated PRs/commits (idempotency + checkpointing proven).
- An approval gate holds for >1h with no pod keeping state, then a Signal resumes it.
- SRE runs on a Temporal Schedule; missing a tick is visible and recoverable.
- LLM rate‑limit → Activity auto‑retries with backoff; no custom retry code.
- `DEVAI_WORKFLOW_PROVIDER=inproc` runs the same pipelines locally with no Temporal
  server (tests green); `noop` degrades cleanly.
- Workflow history payloads are encrypted at rest (codec verified).

## Sources

- [Durable Execution meets AI — why Temporal is ideal for AI agents · Temporal](https://temporal.io/blog/durable-execution-meets-ai-why-temporal-is-the-perfect-foundation-for-ai)
- [Of course you can build dynamic AI agents with Temporal · Temporal](https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal)
- [Temporal for AI Agent Workflows · CallSphere](https://callsphere.ai/blog/temporal-ai-agent-workflows-durable-execution-workflow-as-code)
- [Replay 2026 announcements (Workflow Streams, Agent SDK sandbox) · Temporal](https://temporal.io/blog/replay-2026-product-announcements)
