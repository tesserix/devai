# Sandbox capability review — and what's missing for a customer agent platform

Companion to `IMPLEMENTATION-PLAN.md`. That document set the target; this one records
what the code actually does today (2026-08-14), where it diverges from the target, and
the metric surface the platform has to capture before any of it is worth showing.

The goal being reviewed against:

```
BUILD (ADK) → SANDBOX (run it) → TEST/EVALUATE (score it) → COMPARE (vs baseline)
  → GATE → PROMOTE (registry) → OBSERVE
```

---

## 1. What is actually built

### Shipped and working

| Capability | Where | Notes |
|---|---|---|
| Sandbox contract | `sandbox/models.py` | `SandboxSpec` frozen, `extra="forbid"`; pins agent+version, model, prompt, dataset, tool policy, limits, TTL. Defaults are safe — unclassified tool ⇒ `MOCK`. |
| Persistence + ownership + TTL reaper | `sandbox/service.py`, `sandboxes` table (`tesserix-k8s` devai_db.sql:700) | Foreign sandbox reads as 404. Destroyed rows retained, so eval history survives destruction. |
| Provisioning on the existing Job runtime | `sandbox/provisioner.py`, `job.py`, `isolation.py` | Namespace quota/LimitRange/default-deny NetworkPolicy/scoped SA. No second runtime — correct call. |
| Tool gateway | `sandbox/gateway.py` | real/mock/replay/block; `real` default still blocks side-effecting tools without an explicit per-tool override. Redacts args and responses. |
| Gateway wiring | `agentruntime/runner.py:143` (built-ins), `mcphub/hub.py:298` (MCP) | Both legs go through the same policy; outside a sandbox the gateway is `None` and production dispatch is untouched. |
| Egress control | `sandbox/egress.py`, `egress_proxy.py` | Default-deny plus a declared `allow_domains` list. |
| Credential broker | `sandbox/broker.py`, `broker_github.py` | Narrowest-scope minting, bounded by `allow_scopes`, authenticated by a per-sandbox capability token. |
| Workspace / shell / files / preview / IDE | `workspace*.py`, `preview.py`, `ide_proxy.py` | A person can take a run over in code-server. |
| Dashboard | `dashboard/src/app/sandboxes/` | List + detail: status, TTL, diff, tree. |

That is a genuinely solid Phase 1 — better isolation than the PDF asks for at this stage.

### The review's main finding

**What shipped is a workspace sandbox, not yet an agent-under-test sandbox.**

Everything above answers *"where does an agent's code-writing run happen safely?"*. The
customer-platform question is a different one: *"is this agent version any good, and can
I prove it before promoting it?"* For that, the sandbox has to be something you talk to,
watch, and score. Today it is something you provision and then look at the filesystem of.

Concretely, `sandbox/routes.py` exposes create / get / list / destroy / credentials /
shell / files / preview / ide — and **no `POST /{id}/invoke` and no `GET /{id}/traces`**.
The two endpoints the whole developer loop hangs off are the two that don't exist.

---

## 2. The five structural gaps

### Gap 1 — Publishing to the registry does not make an agent runnable *(the big one)*

There are two unrelated descriptions of an agent in this codebase:

| | Shape | Runnable? |
|---|---|---|
| ADK / registry | `adk/builders.py::Agent` → aregistry envelope (image, skills, prompts, mcpServers) | **No.** It is catalog metadata. |
| Runtime | `specializations/*.yaml` → `Specialization` → `agentruntime/spec_agent.py::SpecAgent` | **Yes.** Carries tools, handover schema, context keys. |

And `specializations/service.py:66-94` is explicit about it: with a registry client
configured, the catalog is *consulted* — `list_skills()` / `list_agents()` validate and
augment — but the returned registry is still `SpecializationRegistry.from_directory(...)`.
Local YAML on disk is the only thing that ever produces a runnable agent.

So the promised loop breaks at both ends. `devai adk new-agent … && devai adk publish`
gets you a catalog entry that nothing can execute; and after a sandbox proves an agent
good, "promote to registry" doesn't deploy it anywhere. A customer agent platform needs
the registry to be **the** source of truth for execution, with the runtime hydrating a
runnable agent from a published, versioned artifact. This is the same convergence noted
after the 2026-06-12 review (dispatcher ↔ registry); it is now the blocking item.

### Gap 2 — No invocation surface

No way to send one turn to a pinned sandbox and get an answer back. Without it there is
no playground, and — more importantly — no eval runner, because an eval is just N
invocations against a dataset.

### Gap 3 — No trace spine (#182)

`ToolCallRecord` exists and the gateway takes a `sink`, but nothing supplies a sink and
nothing persists. `InstrumentedLLMAdapter` already computes tokens and cost per LLM call
into `agent_executions.llm_cost_usd`, and the gateway already has latency, mode, blocked
and error per tool call. **The data is being produced and thrown away.** There is no
object that stitches prompt → LLM → tool → LLM → response into one trace.

Per the plan's own principle: the trace is the deliverable, the score is only an index
into it. Right now we can ship neither.

### Gap 4 — No eval engine

No datasets, no scorer registry, no trajectory scorer, no judge, no comparison, no gates
(#184–#190 all open). What exists — `pipeline/stages/evals.py::score_run` — scores a whole
*ALM pipeline run* on three coarse dimensions (delivered / gates_clean / completion). It is
useful and should stay, but it is not per-agent evaluation and cannot answer "did v17
improve over v16".

### Gap 5 — Mocks are not authorable, replay is not durable

`ToolGateway._mock` falls back to `[mock] <tool> was not executed … args_digest=…` because
`fixtures` is only ever populated in-process. `_recording` likewise lives and dies with the
process, so record-then-replay across two runs is not possible. Mock fixtures need to
become a first-class, versioned artifact next to datasets.

---

## 3. Metrics — what to capture, where it comes from, what to show

This is the part that decides whether the platform looks credible. Rule: **every metric is
captured at the layer that owns the fact, and derived upward.** Nothing is recomputed by
the UI.

### Layer 1 — per LLM call (`trace_spans`)

| Metric | Source | Why |
|---|---|---|
| provider, model, prompt version | `InstrumentedLLMAdapter` / sandbox env | Without it a comparison is meaningless. |
| tokens in / out / total | `LLMUsage` (`adapters/llm/base.py:138`) | Cost and context-pressure. |
| cost_usd | `instrumented.py` price table | Rolls up to per-run and per-tenant budget. |
| latency_ms, ttft_ms | adapter | P95 is a promotion gate. |
| finish_reason, error, retries | adapter | Distinguishes "wrong" from "truncated" from "provider flaked". |
| cache_read / cache_write tokens | adapter | Real driver of cost deltas between prompt versions. |

### Layer 2 — per tool call (`trace_spans`, kind=`tool`)

| Metric | Source |
|---|---|
| tool, arguments (redacted), mode, blocked | `ToolCallRecord` — already produced |
| latency_ms, error | `ToolCallRecord` |
| result size / truncated | gateway (add) |
| mcp server + upstream tool | `mcphub` |
| **was it in the allowlist / was it side-effecting** | `gateway.is_side_effecting` |

### Layer 3 — per invocation (`invocations`)

Rolled up from the spans of one turn: total tokens, total cost, wall-clock, LLM-call count,
tool-call count, distinct tools, blocked-call count, error count, loop/iteration count,
final-state (`completed | limit_exceeded | error | blocked`), and the limit headroom
(`tokens_used / limits.max_tokens`, same for cost and wall clock).

### Layer 4 — per eval case (`eval_case_results`)

| Dimension | Scorer type | Metric |
|---|---|---|
| Correctness | deterministic | exact/JSON/schema/regex match, pass/fail |
| Task completion | deterministic | did it reach the stated goal |
| Tool selection accuracy | trajectory | chose the expected tools |
| Tool order / arguments | trajectory | sensible order, correct params |
| Redundant tool calls | trajectory | calls not needed for the goal |
| Failure recovery | trajectory | recovered after a tool error |
| **Forbidden-action attempts** | trajectory | attempted a `BLOCK`ed tool — the safety metric |
| Groundedness, helpfulness, reasoning, completeness | LLM judge | 0..1, with the judge model + prompt version pinned |
| Hallucination rate | judge | derived: unsupported-claim cases / total |
| Latency, tokens, cost | operational | from layer 3 |

Every case result carries its `trace_id`. A failing case that can't be opened as a trace is
a bug in the product, not a missing feature.

### Layer 5 — per eval run (`eval_runs`)

Success rate · task completion · tool accuracy · groundedness · hallucination % ·
**safety % (must be 100)** · P50/P95/P99 latency · avg tokens · avg cost/run · total run
cost · flake rate (variance across repeats of the same case) · duration. Pinned alongside:
agent version, model, prompt version, dataset version, tool policy hash, scorer set version.

Present exactly as the PDF does — the dimension table, then the one-line verdict:

> Candidate improves quality +5%, but increases cost +33% and latency +22%.

### Layer 6 — comparison / gate

Per-dimension delta baseline → candidate, per-case regression list (passed before, fails
now — this is the actionable bit), and the gate verdict against thresholds:

```
success ≥ 95%   safety = 100%   hallucination ≤ 2%   P95 < 3s   cost ≤ $0.05/run
```

### Layer 7 — platform / tenant (fleet health)

Sandboxes live / created / reaped · avg lifetime vs TTL · spend per tenant, per agent, per
model against budget · blocked-tool attempts per agent (a security signal, watch it) ·
credential grants minted and their scopes · eval runs per week and pass-rate trend per
agent version · promotion outcomes (gated vs shipped) · time from create-sandbox to
promote (the platform's own cycle-time metric).

### Storage

Postgres for transactional metadata (`sandboxes`, `invocations`, `eval_datasets`,
`eval_runs`, `eval_case_results`); the object-store adapter for large trace payloads and
dataset blobs; the telemetry adapter (otel) as an *additional* sink, never the primary —
collectors sit at 0 replicas and traces must not depend on them being up. Schemas go in
`tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/devai/devai-db/` per repo rule 5.

---

## 4. Recommended sequence

Ordered by what unblocks the most, not by phase number.

1. **Registry-as-runtime-source (Gap 1).** One agent shape. A published, versioned artifact
   hydrates into something `AgentDispatcher` can run. Until this lands, "build in the ADK →
   promote to the registry" is not a real loop, and everything below it evaluates artifacts
   that production doesn't actually consume.
2. **Trace spine (#182)** — `invocations` + `trace_spans`, gateway `sink` wired, LLM adapter
   sink wired, `GET /api/sandboxes/{id}/traces`. Cheap, because both producers already exist.
3. **`POST /api/sandboxes/{id}/invoke` (#183 surface)** — one turn, returns a trace id.
   Playground and eval runner both sit on this one endpoint.
4. **Datasets + fixtures as versioned artifacts (#184, Gap 5)** — published like any other
   registry artifact; mocks and replay recordings live here.
5. **Eval runner + deterministic and trajectory scorers (#185, #186).** Trajectory before
   judge: it is cheaper, deterministic, and it is the dimension that actually differentiates
   an agent platform.
6. **LLM judge (#187)** — provider-agnostic through the adapter and `PrincipalLLMResolver`,
   judge model + prompt version pinned into every score.
7. **Compare + gates (#189, #190)**, then wire the gate into publish and into Kargo promotion.
8. **UI last (#191)** — Playground · Traces · Evaluations · Compare on the agent page.
   Four tabs, not a portal.

Items 2–3 are the MVP boundary from the plan; item 1 is the prerequisite the original plan
assumed away.

## 5. Acceptance for "customer agent platform"

An engineer with no Kubernetes knowledge can: author an agent in the ADK (CLI or dashboard),
publish it as a versioned artifact, create a sandbox pinned to that version, chat with it,
watch a side-effecting tool get mocked or blocked, open the full trace with tokens, latency
and cost per step, run an eval suite over a versioned dataset, see per-case pass/fail with
the failing trace one click away, compare it against the production version on the same
dataset, and promote it — with promotion refused automatically on a regression, and every
number still there after the sandbox has reaped itself.
