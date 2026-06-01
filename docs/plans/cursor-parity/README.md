# Cursor‑Parity Plans for DevAI

Goal: derive the **logic, framework and concepts** behind Cursor's agentic
features and re‑implement equivalents inside DevAI's existing architecture
(LangGraph orchestration, the adapter pattern, the K8s Job runner, A2A, the SCM
layer, agent memory, blueprints/stages, the agent registry and the agentgateway).

DevAI is **not** an IDE — it is an autonomous ALM + SRE control plane. So we don't
copy Cursor's editor UX; we copy the **engineering primitives underneath it** and
point them at DevAI's job: ingest work → plan → implement → review → ship →
operate, across many repos, unattended.

> **Foundation: [00 — Temporal durable execution](00-temporal-orchestration.md).**
> Every capability below runs as Temporal **Workflows** (deterministic
> orchestration) calling **Activities** (the side effects — LLM calls, tools, K8s
> Jobs, with LangGraph reasoning *inside* activities). This is what makes the
> whole thing prod‑ready: crash‑safe resume, declarative retries/timeouts, durable
> human‑in‑the‑loop approvals, managed schedules, and exactly‑once side effects.
> LangGraph is kept — Temporal wraps it; it does not replace it. Read plan 00 first.

Every capability below already has a natural home in the codebase. Nothing here
needs a green‑field rewrite — each plan slots into an existing subsystem
(`src/devai/<subsystem>/`) and, where it touches an external system, goes through
`src/devai/adapters/<family>/` per the mandatory adapter rule in `CLAUDE.md`.

---

## How Cursor's pieces map onto DevAI

| # | Cursor capability | Core concept to steal | DevAI home | Priority |
|---|---|---|---|---|
| **00** | **(Temporal — durable backbone)** | Durable execution: checkpointed workflows, durable retries/timeouts, signals for human‑in‑loop, schedules | new `adapters/workflow` + `src/devai/orchestration/` | **P0 (first)** |
| 01 | Codebase indexing (`@codebase`) | Merkle‑diff sync + AST chunking + per‑chunk embeddings in a vector DB | new `adapters/vector_store` + `src/devai/indexing/` | **P0** |
| 02 | Background / Cloud Agents | Ephemeral isolated VM per task → clone, work, open PR; worktree isolation for parallelism | existing `runner/` + `runtime/` (K8s Jobs) + git worktrees | **P0** |
| 03 | BugBot (event‑driven review + Autofix) | Webhook → agent reasons over diff, pulls extra context, multi‑model verify → inline comments → autofix commit | `webhook/` + `agents/` (StaffReviewer, SecurityExpert) + runner | **P0** |
| 04 | Plan Mode | Agent drafts a reviewable, editable plan **before** writing code; human edits/approves | `blueprint/` + `pipeline/` (Fiber stages) + approval gates | **P1** |
| 05 | Memories | Auto‑maintained, cross‑session knowledge base of conventions/preferences | existing `adapters/memory` (episodic/semantic/procedural) | **P1** |
| 06 | Rules | Scoped, reusable system instructions that steer every agent | `specializations/` + new `rules/` loader | **P1** |
| 07 | Tool‑call safety classifier + sandboxing (v3.6) | A classifier subagent gates Shell/MCP/Fetch calls: allowlist safe, sandbox the rest | `tools/` + new `safety/` classifier + runner sandbox | **P1** |
| 08 | MCP tool ecosystem | Plugin protocol to mount external tools/data; budget the tool surface | `adapters/` + `agentic/` agentgateway as MCP host | **P2** |
| 09 | Parallel multi‑agent | N agents on slots, each isolated, merge results | `graph/a2a` + runner worktrees + collaborative team model | **P1** |
| 10 | Automations + `/loop` | Scheduled / repeated / wake‑condition agents, multi‑repo & no‑repo | new `scheduler/` + cron + pipeline triggers | **P2** |
| 11 | Ticketing (`@Cursor` in Jira) | Mention in a ticket → scope from ticket+repo → cloud agent → PR linked back | new `adapters/ticketing` (jira, linear, github_issues) | **P2** |
| 12 | Tab / next‑edit (IDE‑specific) | Low‑latency next‑edit prediction over local context | `runtime/` editor‑bridge — **optional**, low fit | P3 |
| 13 | Shared Canvases | Read‑only shareable live artifact snapshots | `dashboard/` + object store | P3 |

P0 = foundational (everything else leans on it). P1 = the agent‑quality core.
P2 = reach/automation surface. P3 = optional / low fit for a non‑IDE product.

---

## Recommended build order (dependency‑aware)

```
Phase 0 (backbone)        00 Temporal: adapter + worker + port ALM/SRE pipelines
Phase A (foundations)     01 Indexing → 02 Background Agents → 07 Safety classifier
Phase B (agent quality)   06 Rules → 05 Memories → 04 Plan Mode → 09 Parallel agents
Phase C (autonomy loop)   03 ReviewBot/Autofix → 10 Automations → 11 Ticketing
Phase D (ecosystem)       08 MCP host → 13 Canvases → 12 Tab (if ever)
```

Rationale: **Temporal (00) comes first** — port the existing ALM + SRE pipelines
onto durable workflows so everything built after inherits crash‑safety, retries,
durable approvals and schedules for free. Then retrieval (01) and isolated
execution (02) make every agent useful and safe; the safety classifier (07) is
the gate that lets 02 run untrusted tool calls unattended. Rules (06) + Memories
(05) raise output quality for *all* agents at once. Plan Mode (04, a durable
Signal wait) and parallelism (09, child workflows) build on that. ReviewBot (03)
is the flagship autonomous loop that ties everything together — the most direct
"DevAI === Cursor for the whole SDLC" demo.

## Temporal integration matrix (how each plan uses 00)

| Plan | Temporal primitive it leans on |
|---|---|
| 01 Indexing | Activities (`embed`/`upsert`); full re‑index = heartbeating child workflow |
| 02 Background agents | one Workflow per task; `launch_job`/`await_job` Activities |
| 03 ReviewBot | review Workflow; autofix = child workflow per finding; re‑review via Signal |
| 04 Plan Mode | Workflow waits on an **approval Signal** (durable, days‑long) |
| 05 Memories | `learn`/`recall` Activities; auto‑capture post‑run Activity |
| 06 Rules | cached load+assemble Activity |
| 07 Safety classifier | `classify` Activity per tool call; escalate = wait on Signal |
| 08 MCP | MCP calls as retried/audited Activities |
| 09 Parallel agents | **child workflows** fanned out, parent awaits → merge Activity |
| 10 Automations | **Temporal Schedules** + Signals (wake); `continue_as_new` for long loops |
| 11 Ticketing | mention → `start_workflow`; PR‑link write‑back Activity |

---

## Conventions every plan follows

- **Adapters, always.** Any new external dependency (vector DB, ticketing, MCP
  server) is a `adapters/<family>/` with an ABC + factory + lazy SDK import +
  mandatory `noop`. See `CLAUDE.md` → "Adapter Pattern".
- **Settings.** One `DEVAI_<FAMILY>_PROVIDER` env var + per‑backend creds,
  documented in `config.py`.
- **Degrade, never crash.** Missing index / classifier / ticketing → Noop, the
  pipeline keeps running.
- **Schemas live in `tesserix-k8s`** (`db-schema-bootstrap`), never as raw SQL in
  this repo.
- **Contract tests.** Each new adapter family ships `tests/unit/test_<family>_adapters.py`.

Individual plans live alongside this file:

- [`00-temporal-orchestration.md`](00-temporal-orchestration.md) ← **read first**
- [`01-codebase-indexing.md`](01-codebase-indexing.md)
- [`02-background-agents.md`](02-background-agents.md)
- [`03-reviewbot-autofix.md`](03-reviewbot-autofix.md)
- [`04-plan-mode.md`](04-plan-mode.md)
- [`05-memories.md`](05-memories.md)
- [`06-rules.md`](06-rules.md)
- [`07-tool-safety-classifier.md`](07-tool-safety-classifier.md)
- [`08-mcp-host.md`](08-mcp-host.md)
- [`09-parallel-agents.md`](09-parallel-agents.md)
- [`10-automations-loop.md`](10-automations-loop.md)
- [`11-ticketing.md`](11-ticketing.md)

> Tier‑3 (12 Tab, 13 Canvases) are intentionally left as stubs in the table above
> — low fit for a non‑IDE control plane; revisit only if DevAI grows an editor surface.

---

## Production‑readiness checklist (applies across all plans)

Driven by the Temporal backbone (00) + the adapter discipline:

- [ ] **Durability** — every pipeline/agent run is a Temporal Workflow; killing a
      worker mid‑run resumes with no duplicated side effects.
- [ ] **Idempotency** — PR/commit/ticket/deploy Activities take an idempotency key
      (workflowID+attempt); retries never double‑post.
- [ ] **Retries & timeouts** — declarative `RetryPolicy` + `start_to_close` /
      `heartbeat` per Activity; no bespoke retry loops left in business logic.
- [ ] **Human‑in‑the‑loop** — approvals/escalations are durable Signal waits, not
      in‑memory state.
- [ ] **Safety** — every Shell/MCP/Fetch tool call passes the classifier (07);
      risky calls sandboxed in locked‑down Jobs; all decisions audited.
- [ ] **Degrade, never crash** — each adapter family (`workflow`, `vector_store`,
      `ticketing`, `mcp`, `memory`, …) has a Noop fallback; missing backend = degrade.
- [ ] **Multi‑tenancy** — Temporal Namespace + per‑tenant config; task queues per
      concern (`alm`/`sre`/`review`/`index`/`background`).
- [ ] **Secrets & encryption** — Temporal payload codec encrypts history; creds via
      the `secrets` adapter (GCP SM); never plaintext prompts/code at rest.
- [ ] **Observability** — Temporal UI history + Prometheus/OTel metrics + LangSmith
      traces inside Activities; run IDs persisted to `pipeline_runs`.
- [ ] **Schemas in tesserix‑k8s** — all new tables (`code_chunks`, `plan_artifacts`,
      `review_findings`, `automations`, …) added to `db-schema-bootstrap`, never raw
      SQL in this repo.
- [ ] **Contract tests** — every adapter family passes one shared suite proving the
      swap is real; integration tests use Temporal's time‑skipping test env.
- [ ] **Deployment** — Temporal server + `devai-worker` as ArgoCD apps; local via
      `sandboxctl` + `values-local.yaml`; HPA/KEDA on workers per task queue.
