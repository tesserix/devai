# 02 — Background / Cloud Agents

**Cursor parity:** Background Agents (cloud VM per task). **Priority: P0.**

> **Durability ([00 Temporal](00-temporal-orchestration.md)):** each task is **one
> Temporal Workflow**; `launch_job` + `await_job` (heartbeating) are Activities, so
> a worker crash resumes the run and never double‑opens a PR. Worktree
> setup/teardown are Activities with idempotency keys.

## What Cursor does

Spins up an ephemeral cloud machine that clones the repo, checks out a branch,
does the work autonomously (edit/build/test/iterate), and opens a PR — without
touching the user's laptop. Triggerable from IDE, Slack, or web. Parallel agents
each get an isolated **git worktree** so they never collide.

## How it works (the concepts to steal)

1. **Ephemeral, isolated execution unit per task.** Clean environment, repo
   cloned fresh, branch checked out, network/tooling provisioned. Cursor uses
   Ubuntu VMs; **DevAI already does this with K8s Jobs** (`runner/` + `runtime/`,
   the SDK/ADK plan — agents run as Jobs). This plan is mostly *completing* it.
2. **Worktree isolation for parallelism.** Each agent edits its own `git worktree`
   so builds/tests don't step on each other; results merge independently.
3. **Full agent loop inside the unit.** Run command → read output → react to
   errors → iterate until tests pass. Then push branch + open PR.
4. **Multi‑trigger.** Same execution unit launched from webhook, chat, schedule,
   or ticket — the *trigger* is decoupled from the *runtime*.

## DevAI mapping (framework)

- **Runtime already exists:** `DEVAI_K8S_RUNTIME_ENABLED`, `DEVAI_RUNNER_IMAGE`,
  `runner/`, `runtime/`, `devai-runner`/`devai-editor-bridge` images, RBAC
  templates. This plan hardens it into a first‑class "background agent" surface.
- Add **git‑worktree isolation** in the runner: one worktree per Job, cleaned on
  completion (mirror our own Agent tool's `isolation: worktree`).
- A **Job spec builder** (`runner/jobspec.py`) that maps an Agent contract +
  task → a K8s Job (image, env, repo, branch, resource limits, TTL — most knobs
  already in the chart).
- **Trigger fan‑in:** `webhook/`, `chat/`, `scheduler/` (plan 10), `ticketing`
  (plan 11) all enqueue the same `BackgroundTask` → one dispatcher.
- **Lifecycle + status** persisted to `pipeline_runs` / `agent_executions`,
  streamed to the dashboard (we already render runs/agents).
- Output is a **PR** via the `scm/` layer (GitHub/GitLab/ADO already abstracted).

## Implementation plan

- **Phase 1 — task contract.** `BackgroundTask` model (repo, branch, prompt,
  blueprint, trigger, budget). One `dispatch(task)` entry point.
- **Phase 2 — Job runtime hardening.** `jobspec.py`, worktree init/teardown,
  TTL + backoff (chart values exist), pull‑secret + SA wiring.
- **Phase 3 — agent loop in‑Job.** Runner executes the blueprint, captures
  build/test output, iterates within `DEVAI_MAX_*_ITERATIONS`.
- **Phase 4 — PR emission + status.** Push branch, open PR via `scm/`, write
  status back, stream to dashboard, A2A `handoff` to ReviewBot (plan 03).
- **Phase 5 — parallel slots.** N concurrent Jobs, each its own worktree (feeds
  plan 09).

## Files & modules

```
src/devai/runner/{jobspec,worktree,lifecycle}.py
src/devai/runtime/background.py          # dispatch() + status
src/devai/models.py                      # BackgroundTask
tests/unit/test_runner_jobspec.py
helm: helm/devai + tesserix-k8s/charts/apps/devai-api (runner RBAC already present)
```

## Config (`DEVAI_*`)

```
DEVAI_K8S_RUNTIME_ENABLED=true
DEVAI_RUNNER_IMAGE=ghcr.io/tesserix/devai/devai-runner:main
DEVAI_K8S_JOB_TTL_SECONDS=3600
DEVAI_K8S_JOB_BACKOFF_LIMIT=0
DEVAI_RUNNER_WORKTREE_ISOLATION=true
DEVAI_BACKGROUND_MAX_PARALLEL=8
```

## Acceptance criteria

- `dispatch(task)` creates a K8s Job that clones, branches, runs the blueprint in
  an isolated worktree, opens a PR, and reports status — laptop‑free.
- Two tasks on the same repo run concurrently without file collisions.
- Job TTL cleans up; failure surfaces in `agent_executions` with logs.
- Runtime disabled → falls back to in‑process execution (degrade, don't crash).

## Sources

- [The Harness Is the Product: Cursor Cloud Agents' Architecture](https://cozypet.github.io/cursor-cloud-harness/)
- [Best practices for coding with agents · Cursor](https://cursor.com/blog/agent-best-practices)
- DevAI: `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md`
