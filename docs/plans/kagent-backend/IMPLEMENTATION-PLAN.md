# kagent as a runtime backend — wire the deployed controller into dispatch

**Status:** Track A implemented — 2026-06-16 (Track B still planned). The DevAI-side
A2A routing now ships: `JobRunnerStage._maybe_dispatch_kagent` routes agents labelled
`devai.io/runtime=kagent` through `KagentClient` and degrades to the Job path on any
error. Remaining: label a real agent + verify the controller's A2A contract in-cluster.

> **What landed (Track A):** `registry/client.py` surfaces `metadata.labels` on the
> `Agent` model; `agentic/kagent_client.py` adds `RUNTIME_LABEL`/`extract_a2a_text`;
> `pipeline/stages/job_runner.py` routes before the Job path; tests in
> `tests/unit/test_job_runner_kagent.py`. `DEVAI_KAGENT_URL` was already wired in the
> `devai-api` chart, and `/v0/export/kagent` (agentic-registry) + `kagent-agent-sync`
> already reconcile labelled agents into Deployments. Nothing is labelled yet, so
> behavior is unchanged until an operator opts an agent in.

---

**Original plan (sequenced after `docs/plans/agent-harness/`)**
**Goal:** make kagent a real, opt-in execution path for **long-lived** agents,
without disturbing the default per-run **K8s Job** path. kagent is already deployed
and a client + route already exist — this finishes the wiring deliberately, for the
cases that actually want it.

---

## Current state (audit)

kagent is **deployed and half-wired**, intentionally off the hot path:

| Piece | Where | State |
|---|---|---|
| Controller (solo.io, `kagent-system`, :8083) | `tesserix-k8s/charts/thirdparty/kagent/`, ArgoCD `kagent-crds.yaml` + `kagent.yaml` | running |
| Reconciler CronJob (registry agents → Deployments) | `tesserix-k8s/charts/apps/kagent-agent-sync/` | running |
| DevAI A2A client (`message/send` → `/api/a2a/{ns}/{agent}`) | `src/devai/agentic/kagent_client.py` | built, identity-forwarding, SSRF-guarded |
| Dispatch endpoint `POST /agentic/kagent/{agent}/dispatch` | `src/devai/agentic/routes.py:122` | built, operator-gated |
| Settings `kagent_url` / `kagent_default_namespace` | `src/devai/config.py:623-638` | present, **`kagent_url` defaults to `""`** |
| Default execution path | `pipeline/stages/job_runner.py` → `runtime/job_spec.py` → `runtime/job_watcher.py` | **one K8s Job per agent run** |

So today: `kagent_url=""` → `create_kagent_client()` returns `None` → the dispatch
route 503s. Every agent runs as an ephemeral Job. That is the **right default** —
short-lived, fan-out, per-story work maps cleanly to Jobs. kagent fits a *different*
shape: standing, controller-managed, queryable agents.

> Note: the broader `ExecutionBackend` ABC + `backends/{inline,job,kagent}.py`
> described in `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md` is **not built** (no
> `ExecutionBackend` in the tree). This plan therefore offers a narrow Track A that
> needs no refactor, and a Track B that lands the seam properly later.

---

## When to use kagent (the decision rule)

- **Job (default):** a unit of work that starts, produces a result, exits — every
  ALM stage, SRE monitor sweep, per-story implement chain. Fan-out = `dispatch_many`.
- **kagent (opt-in):** a *standing* agent that should stay resident and be addressable
  over A2A between runs — e.g. a long-running SRE responder, a always-on "ask the
  repo" agent, or an externally-callable agent another system hands tasks to.

Pick kagent for **lifecycle**, not for throughput.

---

## Track A — finish the existing opt-in path (small, no refactor)

Make the already-built client+route real for explicitly-marked agents.

1. **Mark intent in the seed.** Extend the agent spec `runtime.kind` to allow
   `kagent` (alongside `python_class` / `yaml-only`). A `kagent`-runtime agent is one
   the reconciler should stand up as a controller-managed Deployment.
2. **Reconcile registry → kagent CRDs.** `kagent-agent-sync` (in `tesserix-k8s`)
   translates each `runtime.kind: kagent` registry Agent into a kagent `Agent` CRD
   (model, prompt, MCP/tool refs). This is the missing half — the CronJob exists but
   must learn the registry→CRD mapping.
3. **Route marked agents through the client.** In the dispatcher/`job_runner` seam,
   if the resolved profile is `runtime.kind: kagent` **and** `settings.kagent_url` is
   set, call `KagentClient.dispatch(agent, message, triggered_by, trace_id)` instead
   of submitting a Job. Identity already forwards on `X-Forwarded-User`/`X-Trace-Id`
   with the `X-Auth-Bff-Secret` trust stamp (`kagent_client.py:104-113`).
4. **Set `kagent_url`** in `tesserix-k8s` `devai-api` values (local first via
   `connect-local` + `values-local.yaml`, per CLAUDE.md rule 4a) — point at
   `http://kagent-controller.kagent-system.svc.cluster.local:8083`. Confirm the
   controller's A2A contract in-cluster before enabling on any hot path (the client
   docstring flags this explicitly).
5. **Degrade, never crash.** `create_kagent_client` already returns `None` when
   unconfigured; a dispatch failure must fall back to the Job path (same Noop-style
   contract as the adapters), logged.

Acceptance (Track A): one agent marked `runtime.kind: kagent` is reconciled into a
kagent Deployment, a trigger dispatches to it over A2A, the result flows back through
the same RESULT path, and the dashboard attributes it to the triggering user.

---

## Track B — the proper backend seam (later, lands the ABC)

Implement the deferred dispatcher seam so backend choice is one config switch:

- `src/devai/adk/backends/base.py` — `ExecutionBackend` ABC: `async run(agent_name, ctx) -> AgentResult`.
- `backends/inline.py` (dev/fallback), `backends/job.py` (wraps `job_spec` +
  `job_watcher` — today's default), `backends/kagent.py` (wraps `KagentClient`).
- `AgentDispatcher` picks the backend per profile: `runtime.kind` + a
  `DEVAI_EXECUTION_BACKEND` override; unknown/unavailable → Job (graceful degrade).
- This is the `ExecutionBackend` from the SDK/ADK plan §7 — kagent slots in with
  **zero change** to the SDK contract or callers. Build it when more than one agent
  wants kagent, or when we want lifecycle/queueing/retries managed for us.

---

## Why sequence this after the agent-harness

A standing, externally-addressable kagent agent is a *bigger blast radius* than an
ephemeral Job: it persists, it's reachable over A2A, it holds credentials between
runs. It should only go live **after** the publish harness
(`docs/plans/agent-harness/`) can prove an agent's refs resolve, its tools/MCP grants
are tenant-owned and not over-broad, and its risk level is gated. Harness first makes
the artifact trustworthy; kagent then gives the trustworthy artifact a long life.

## Test + deploy
- Unit: `runtime.kind: kagent` routing decision; fallback-to-Job on client `None` /
  dispatch error; reconciler registry→CRD mapping (in `tesserix-k8s`).
- Track A ships behind `kagent_url` empty-by-default — zero prod impact until set.
- All cluster changes via ArgoCD/`tesserix-k8s`; local via `connect-local` +
  `sandboxctl` (CLAUDE.md rules 4 / 4a). No `kubectl apply`.
