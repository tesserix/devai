# Phase 5 — Durability: enabling Temporal + NATS WorkQueue

**Status: the code is complete, tested, and off by default.** This is the
runbook to turn it on. Both backends are opt-in config flags; the work is
deploying the infra they need (a tesserix-k8s / ops task, not a devai change).

## What's already built (no code work needed)

| Piece | Where | Default |
|---|---|---|
| Temporal worker — generic `BlueprintWorkflow` + `run_stage` activity (runs *every* blueprint) | `src/devai/orchestration/worker.py`, `workflows.py`, `activities.py` | — |
| Workflow adapter (`inproc` / `temporal` / `noop`) | `src/devai/adapters/workflow/` | `inproc` |
| NATS JetStream WorkQueue dispatch (single-delivery + DLQ) + consume (`subscribe`) | `src/devai/pipeline/dispatch.py` | — |
| Dispatch backend selector (`redis` / `nats` / `inproc`) | `src/devai/pipeline/pipeline.py` | `redis` |

Tests: `tests/unit/test_workflow_adapter.py`, `test_dispatch_workqueue.py`, `test_pipeline_durable_queue.py`.

## Enable Temporal (crash-safe per-stage resume)

1. **Deploy a Temporal cluster** in `tesserix-k8s` (Helm `temporalio/temporal`, or Temporal Cloud). Namespace e.g. `devai`.
2. **Deploy the `devai-worker` Deployment** running `python -m devai.orchestration.worker` (same image as `devai-api`). It builds the same `StageDeps` + `StageRegistry` via `build_runtime`, so it runs every blueprint — adding a blueprint/agent never needs a worker change.
3. **Flip the config** in the devai prod values (External Secret / ConfigMap):
   - `DEVAI_WORKFLOW_PROVIDER=temporal`
   - `DEVAI_TEMPORAL_HOST`, `DEVAI_TEMPORAL_NAMESPACE` (default `default`), `DEVAI_TEMPORAL_TASK_QUEUE` (default `devai`), `DEVAI_TEMPORAL_TLS_ENABLED`, `DEVAI_TEMPORAL_MAX_CONCURRENT_ACTIVITIES` (default 50).
4. **Verify:** trigger a run, kill the `devai-api`/worker pod mid-stage — the run resumes from the last completed stage (Temporal replays the workflow; the `run_stage` activity is idempotent per the executor's resume contract).

## Enable NATS WorkQueue dispatch (single-delivery, replaces the Redis claim guard)

1. **Ensure NATS JetStream** is deployed (`DEVAI_NATS_URL` already exists). The backend declares the WorkQueue stream + DLQ on boot.
2. **Flip the config:** `DEVAI_PIPELINE_DISPATCH_BACKEND=nats`. This removes the api↔sre Redis claim-guard ping-pong (lane subjects, native single-delivery).
3. **(Phase 1.5) Worker split + KEDA (optional, for scale):** run the consumer in a dedicated Deployment (the same image; the pipeline `subscribe`s on the lane subjects) and add a **KEDA `nats-jetstream` ScaledObject** on the WorkQueue depth so consumers scale 0→N with backlog. The single-process inline consume works without this; the split is purely for horizontal scale.

## Why this is ops, not code

The durable-execution seam (`pipeline/pipeline.py`) and the WorkQueue selector are already in the hot path; both degrade to the in-process/Redis path when the flags are unset. Flipping a flag **without** the backing infra (Temporal cluster / NATS) would make pods fail to connect — so the cluster + worker must land first. That sequencing + verification is the tesserix-k8s deploy, tracked alongside the other control planes in `docs/deploy/CONTROL-PLANES.md`.
