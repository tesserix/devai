# Agent Sandbox & Evaluation — implementation plan

**Goal:** make the ADK the single authoring path for every agent in DevAI, and give
that path a place to *run and be measured* before anything is published or promoted.

```
BUILD (ADK) → SANDBOX (isolated run) → EVALUATE (scored run) → COMPARE (vs baseline)
  → GATE (thresholds) → PUBLISH/PROMOTE → OBSERVE
```

Today DevAI has the first and the last link. Everything between them is missing or
partial. This plan closes the gap in five phases, reusing the runtime we already have
rather than building a parallel one.

---

## 0. The two concepts, in DevAI terms

### Sandbox

A sandbox is **the same agent runtime as production with different boundaries**. Not a
different code path, not a "test mode" agent. If the sandbox runs a special runtime you
eventually get *works in sandbox, fails in production*, which is worse than no sandbox.

Concretely, one sandbox pins a full, immutable configuration:

| Pinned | Why it must be pinned |
|---|---|
| agent version | otherwise "did v17 improve?" is unanswerable |
| model + provider | a model swap changes every number |
| prompt version | same |
| tool set + per-tool mode | determines what the agent is even allowed to attempt |
| dataset version | comparing across dataset edits is meaningless |
| limits (tokens, cost, wall-clock) | a looping agent must not bill unbounded |
| TTL | sandboxes are ephemeral; 4h default, explicit extend |

And it changes what the agent's actions *reach*. That is the part unique to AI
sandboxes: a normal app sandbox isolates a process, an agent sandbox has to isolate
**side effects**, because the agent decides at runtime to call `refund_customer()`.

### Evals

An eval is a **test suite for non-deterministic software**. A unit test asserts
`f(x) == y`. An agent has no such property, so instead you fix a dataset of cases, run
the agent over all of them, and score each run along several dimensions:

- **Deterministic** — verifiable directly: expected JSON, expected tool call, schema
  valid, task completed, regex match. Cheap, fast, no LLM.
- **Trajectory** — did it pick the right tools, in a sensible order, with the right
  arguments; did it call tools it didn't need; did it recover from a failure; did it
  attempt a forbidden action. **This matters more than the final answer** for an agent
  platform, and it is the dimension DevAI currently scores not at all.
- **LLM-as-judge** — helpfulness, groundedness, reasoning quality, completeness. Use
  it for what deterministic scorers can't reach, not as the default.
- **Operational** — P95 latency, tokens, cost per run, safety/blocked-action rate.
- **Human** — the escape hatch when automated scoring isn't sufficient.

The output of an eval is never just a number. A score of 62% tells an engineer nothing;
**the trace of the failing case tells them everything.** Traces are the deliverable;
the score is the index into them.

---

## 1. What already exists (grounded)

| Capability | Where | State |
|---|---|---|
| Agent authoring | `src/devai/adk/` — `Skill/Agent/Prompt/McpServer` builders, `Publisher`, `scaffold`, `devai adk new-*/validate/publish` | **Works.** Authoring + schema/lint validation only. No run, no test. |
| Agent artifacts | `architecture/registry-seeds/agents/` (40 agents), `specializations/` (26 personas) | **Works.** Built-ins are artifact-described and bridge to Python agents. |
| Agent execution | `src/devai/pipeline/stages/job_runner.py` + `src/devai/runtime/{job_spec,job_watcher,k8s_client}.py` | **Works.** Every agent already runs as an on-demand K8s Job with a pinned image, resolved tools/skills/prompts, and a `RESULT::` protocol. **This is the sandbox runtime — do not build another.** |
| Ephemeral env + TTL reaper | `src/devai/preview/service.py` (4h idle TTL, reaper loop, `reap_expired`) | **Works** for preview pods. The lifecycle pattern the sandbox should copy. |
| Tool allowlist | `src/devai/tools/registry.py` (`bind(allowed_tools, ctx)`), `src/devai/tools/dispatch.py` (`execute()` denies outside the allowlist, logs `tool DENIED`) | **Partial.** Binary allow/deny only. No mock, no replay, no record of args/latency/result. |
| MCP multiplexing | `src/devai/mcphub/` | **Works.** Needs a sandbox profile. |
| Evaluation | `src/devai/pipeline/stages/evals.py` — `score_run()`, `agent_evals` table, `/api/analytics/evals` | **Partial.** One hardcoded scorer over a whole *ALM pipeline run* (PR produced, gates clean, stage completion). No datasets, no per-agent eval, no trajectory, no judge, no comparison. |
| Telemetry | `src/devai/adapters/telemetry/` (otel \| noop) | **Works**, currently parked (collectors at 0 replicas). Traces must not depend on it being up. |
| Dashboard | `dashboard/src/app/{agents,runs,analytics,...}` | No playground, no trace viewer, no eval or compare view. |

**The five real gaps:** sandbox as a first-class object · tool gateway with modes ·
datasets + a scorer registry · trace capture per invocation · comparison and gates.

## 2. Design decisions

1. **The sandbox is a configuration of the existing Job runtime, not a new service.**
   `SandboxSpec` becomes extra env + policy on the `V1Job` that `build_job_spec()`
   already renders. The runner image does not know it is sandboxed.
2. **The tool gateway lands before the eval engine.** Evaluation *executes agents
   against real tool surfaces*; without mode control an eval suite can issue a real
   refund. `dispatch.py` is the chokepoint and already exists.
3. **BLOCK/MOCK is the default for side-effecting tools.** Engineers opt into real.
4. **Eval results outlive the sandbox.** Destroying a sandbox must not destroy history,
   so results go to Postgres (schema in `tesserix-k8s`), never sandbox-local storage.
5. **Everything is versioned and immutable.** agent version, prompt version, dataset
   version, model config. Without that, comparison is not trustworthy.
6. **Provider-agnostic judging.** The judge goes through the LLM adapter and
   `PrincipalLLMResolver` like every other call — never a hardcoded vendor.
7. **Reuse `preview/`'s TTL reaper** rather than writing a second reaper.
8. **No new cluster.** Namespace-scoped isolation on the existing 3-node cluster:
   ResourceQuota, LimitRange, default-deny NetworkPolicy, scoped SA, scoped secrets.
   Stronger isolation (gVisor/Kata) only if the threat model demands it later.

## 3. Phases

**Phase 1 — Foundation.** `SandboxSpec` + `sandboxes` table + REST API + provisioning
through the existing Job runtime + TTL reaper.

**Phase 2 — Developer sandbox.** Playground invoke, the tool gateway with
real/mock/replay/block, and trace capture. *MVP ends here + a slice of Phase 3.*

**Phase 3 — Evaluation.** Datasets, eval suites, the runner, deterministic + trajectory
scorers, then the LLM judge, with results stored independently.

**Phase 4 — Comparison and gates.** Baseline vs candidate on one dataset; thresholds
that block publish and promotion; wired into the agent harness (#75) and Kargo.

**Phase 5 — Scale and governance.** Per-tenant quotas, cost accounting, approval
policy, shared dataset registry, stronger isolation.

## 4. Relationship to the kagent Agent Substrate epic (#69)

#69 asks *where* sandboxed agents run (WorkerPool + Actor vs on-demand Jobs) and is
gated on #70. **This plan is deliberately independent of that outcome.** The sandbox
contract, tool gateway, traces, datasets, scorers, comparison and gates are all
substrate-agnostic — they sit above whatever executes the agent. If Substrate lands,
sandbox provisioning swaps its backend; nothing else moves. #75 (the lifecycle
build→test→security gate) is the natural consumer of Phase 3's output: "test" in that
harness *is* an eval run in a sandbox.

## 5. Data model

```
Agent → AgentVersion → Sandbox → Invocation → Trace → ToolCall
Dataset → DatasetVersion → Case
EvalSuite → EvalRun → CaseResult → Score(scorer, value)
Comparison → (EvalRun baseline, EvalRun candidate) → Delta
```

Postgres holds the transactional metadata (`sandboxes`, `eval_datasets`, `eval_runs`,
`eval_case_results`, extending the existing `agent_evals`). Large traces and dataset
blobs go to the object-store adapter. Schemas live in `tesserix-k8s`
(`charts/apps/db-schema-bootstrap/schemas/devai/devai_db/`) per repo rule 5.

## 6. API surface

```
POST   /api/sandboxes                 create (spec: agent+version, model, prompt,
GET    /api/sandboxes/:id                    tools+modes, dataset, limits, ttl)
DELETE /api/sandboxes/:id
POST   /api/sandboxes/:id/invoke      one turn; returns trace id
GET    /api/sandboxes/:id/traces

POST   /api/evaluations               run a suite against an agent version
GET    /api/evaluations/:id

POST   /api/comparisons               baseline vs candidate on one dataset
GET    /api/comparisons/:id
```

UI comes after the API and stays small: **Playground · Traces · Evaluations · Compare**
on the existing agent page. Not a new portal.

## 7. Security baked in from the start

Sandbox TTL · least-privilege SA · default-deny egress · no production secrets ·
explicit tool permission policy · immutable audit of every tool call · max token budget ·
max monetary budget · rate limits · destructive tools blocked unless explicitly approved.

## 8. Acceptance for the MVP

An engineer can: create a sandbox for an agent version from the dashboard or
`devai adk`, chat with it, watch its tools get mocked or blocked, inspect the full trace
of prompt → LLM → tool → LLM → response with tokens/latency/cost, run a small eval suite
over a dataset, see per-case pass/fail with the failing trace, and have the sandbox
destroy itself on TTL — with the eval results still there afterwards.

---

## Tracking

Epic #192, issues #179–#191, labelled `sandbox` / `evals` / `adk`.
Related: #69 (Agent Substrate), #75 (agent lifecycle harness),
`docs/plans/agent-harness/IMPLEMENTATION-PLAN.md`, `docs/agentic/MCP-HUB.md`.
