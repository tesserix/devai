# Wiring `tesserix-adk` into DevAI

Companion to `PLATFORM-REVIEW.md`. That document said what the DevAI sandbox is missing.
This one covers the library that was released into that gap — `tesserix/agent-development-kit`,
**v0.1.1, tagged 2026-08-14** — what it actually contains, who owns what once both exist,
and the order to wire them.

Verified against the published wheel (`tesserix_adk-0.1.1-py3-none-any.whl`, installed
into a clean venv), not the README.

---

## 0. Two things are called "the ADK", and two things are called "the sandbox"

Fix the vocabulary before anything else, because both collisions will otherwise land in
code:

| Term | Means here |
|---|---|
| **DevAI ADK** (`src/devai/adk/`, `devai adk …`) | Authoring: builders that emit registry YAML, `Publisher`, `scaffold`. Catalog metadata. |
| **The kit** (`tesserix-adk`, `tesserix_adk.*`) | The runtime library: agent definition, run loop, providers, tools, guardrails, cost, tracing. |
| **DevAI sandbox** (`src/devai/sandbox/`) | An isolated K8s namespace + Job with a pinned `SandboxSpec`, TTL, egress policy, credential broker. The *outer* boundary. |
| **Kit sandbox** (`tesserix_adk.tools.sandbox`) | `SubprocessSandbox` — a fresh `-I -S` interpreter with no network, no inherited env, RLIMIT ceilings, for running *model-written code*. The *inner* boundary. |

They are complementary, not competing: the kit's sandbox is what a coding agent's
`run_python` reaches inside a DevAI sandbox pod. Both should keep their names, with the
distinction stated wherever either is exported.

## 1. What v0.1.1 actually ships

142 modules. Substantial and mypy-strict, with a governed API surface
(`docs/api-surface.txt`, 1611 lines; a change there fails CI until the changelog explains
the stability decision).

**Real and usable:**

| Subpackage | What's in it |
|---|---|
| `core` | `Agent` definition (name, version, instructions, model, tools, `idempotent_tools`, `approval_required_tools`, `output_type`, `budget`, `deadlines`, `loop`, `retry`, `repair`, `on_tool_error`, `guardrails`), `Run`/`RunContext`/`RunRecord`, `Usage`/`Cost`, `TenantContext`, `BudgetPolicy`/`SpendLedger`/`Ceiling`, `ModelProvider` protocol + `ModelCapabilities`, `GuardrailPipeline`, `Checkpoint`, `SuspensionStore`, `WorkQueue`, `Idempotency`, `AuditSink`, `AutonomyLadder`, `TrustBoundary`, `Tracer` |
| `runtime` | `AgentRunner` — loop, retry, cancellation, parallel/fan-out, delegation, planner, supervisor, approvals, progress events, estimate, spend, structured output, determinism, rate limiting, handoff, resume |
| `models` | Providers: `openai`, `gemini`, `llama_cpp`, `compatible`; plus routing, pricing/`known_models`, client pool, response cache, embeddings + batching, credentials |
| `tools` | `@tool`, `ToolRegistry`, argument validation, error taxonomy (`ToolFailure` vs `ToolRefusal`), `ToolCallSpan`, claim-check, `SubprocessSandbox`/`sandbox_tool` |
| `memory` | Protocol, records, scope, beliefs/supersession, erasure, compaction, capabilities |
| `observability` | **`RunTree`, `Node`, `Step`, `Totals`, `TraceContext`, `record_tree`, `render`, `Attribution`, `Meter`, `SpendRecord`, `Redactor`** |
| `testing` | `FakeTracer`/`FakeBudgetPolicy`/`FakeToolRegistry`/…, **`Cassette`, `HttpCassette`, `RecordingProvider`, `ReplayingProvider`**, `INJECTION_FIXTURES`, protocol conformance suites, pytest plugin, `NetworkAccessInTestError` |
| `adapters` | Redis / SQL state / ledger / grants / audit / approvals / idempotency / cache / ceiling / graph / memory / transport |
| `cli` | `inspect`, `approvals` |

**Declared but empty (`__all__: list[str] = []`):** `evals`, `workflows`, `rag`, `a2a`,
`mcp`, `experimental`.

**Not shipped and relevant to us:** there is **no Anthropic provider**. DevAI's primary
model plane is Claude, so this is a hard prerequisite, not a detail. `compatible.py` is a
partial escape hatch, not the same thing.

Backlog: 244 issues on the board. `pkg:evals` has 6 open (#152 golden datasets, #153
metric definitions with cost/latency first class, #154 LLM judge with human calibration,
#155 baseline regression + CI gate, #60 prompt changes gated, #241 compression gate);
#202 and #204 were closed `NOT_PLANNED` as duplicates, so **no eval code exists anywhere yet**.

## 2. The decision this forces

DevAI already has its own version of roughly half of the above: `adapters/llm/*` with
`InstrumentedLLMAdapter`, `tools/registry.py` + `dispatch.py`, `adapters/memory/*`,
`adapters/telemetry/*`, `agentruntime/` with `AgentDispatcher`/`SpecAgent`, and the
`sandbox/gateway.py` tool policy.

Wiring the kit in without deciding ownership gives DevAI two run loops, two tool
registries and two cost models. That is worse than not wiring it at all.

**The split:**

> **The kit owns what happens inside one agent run. DevAI owns everything between runs.**

| Concern | Owner | What happens to the DevAI code |
|---|---|---|
| Agent definition, run loop, tool contract, structured output, retries | **kit** | `agentruntime/` becomes a thin host: it builds a `tesserix_adk.core.Agent` and calls `AgentRunner`, instead of driving its own loop. |
| Model providers, routing, pricing, fallback | **kit** | `adapters/llm/*` stays as DevAI's *credential and policy* layer (`PrincipalLLMResolver`, per-user connectors, no-Fable rule) but delegates the call to a kit `ModelProvider`. The chokepoint stays where it is. |
| Tokens / cost / spend ledger | **kit** (`Usage`, `Cost`, `SpendLedger`) | `InstrumentedLLMAdapter`'s price table retires in favour of `models.pricing`; DevAI keeps writing `agent_executions.llm_cost_usd`. |
| Trace spine | **kit** (`RunTree`/`Node`/`Step`/`Totals`) | **Gap 3 closes by adoption, not by building.** DevAI persists the tree and serves it. |
| Record / replay of tool + provider calls | **kit** (`Cassette`, `RecordingProvider`, `ReplayingProvider`) | **Gap 5 closes by adoption.** `gateway._recording` (in-process, dies with the pod) is replaced by a cassette artifact. |
| Guardrails, injection fixtures | **kit** | DevAI's ADK security trio (#97) re-expresses as kit `Guard`s. |
| In-process code execution | **kit** (`SubprocessSandbox`) | New capability for DevAI; nothing to retire. |
| Approvals, autonomy ladder, idempotency, audit | **kit** primitives, **DevAI** storage | DevAI's `approval_gates` table becomes the adapter behind `ApprovalTransport`. |
| Tenancy on the wire | **kit** (`TenantContext`, propagation contract) | DevAI's principal resolvers feed it. |
| **Tool *modes* (real/mock/replay/block)** | **DevAI** (`sandbox/gateway.py`) | Keep. The kit has allowlists and approval gates, not sandbox modes. The gateway wraps the kit's registry. |
| K8s sandbox provisioning, TTL, egress, credential broker | **DevAI** | Unchanged. |
| Registry / catalog / publish / promote | **DevAI** + aregistry | Unchanged. |
| MCP hub, A2A bus | **DevAI** | The kit's `mcp` and `a2a` are empty; DevAI's are built and in production. **DevAI's should become the kit's implementation later, not the reverse.** |
| Blueprints, pipeline, stages, SRE, dashboards | **DevAI** | Unchanged. |
| Datasets, eval runs, comparison, gates | **split — see §4** | |

## 3. Mechanical blockers (all real, none hard)

1. **Python version.** The kit needs `>=3.12` and uses PEP 695 generics
   (`async def run[OutputT: BaseModel]`). DevAI declares `requires-python = ">=3.11"`
   though its image is already `python:3.12-slim`. Bump the declaration. (The in-flight
   3.14 image migration is compatible — the wheel installs and imports on 3.14.7.)
2. **Distribution.** PyPI publishing is **off** in the kit's `release.yml` (waiting on a
   trusted publisher), and the repo is **private**. The supported path is the GitHub
   release assets, which is what its own `smoke-mirror` job exercises:
   `gh release download v0.1.1 --pattern '*.whl'` then
   `pip install --find-links downloaded 'tesserix-adk==0.1.1'`. DevAI's Dockerfile and CI
   both need a read token for `tesserix/agent-development-kit`. Verify the attestation
   (`gh attestation verify --signer-workflow …`) in CI; it is already produced.
3. **Pin exactly.** `==0.1.1`. It is 0.1.x, 0.1.0 shipped breaking changes to
   `ModelProvider`, and the surface is governed but not stable.
4. **Extras.** Base install is `pydantic + httpx + opentelemetry-api` only. DevAI needs
   `[redis,postgres]`; `mcp` and `temporal` extras are for empty subpackages today.
5. **Name collision.** `src/devai/adk/` vs `tesserix_adk`. Rename the DevAI package to
   `src/devai/authoring_kit/` (it is authoring, not a kit) or keep it and never import
   both unqualified in one module. Decide once, up front.

## 4. Where the eval engine goes

The `PLATFORM-REVIEW.md` plan assumed DevAI would build evals. Given the kit's empty
`evals` and its six open issues, build it **once, in the kit**, and orchestrate it from
DevAI:

| Piece | Home | Why |
|---|---|---|
| Dataset + case format, versioning | kit `evals` | Every Tesserix product needs it; testable with no network. |
| Scorer protocol + deterministic scorers | kit `evals` | Pure functions over a `RunTree`. |
| Trajectory scorers (tool choice, order, args, redundancy, recovery, forbidden actions) | kit `evals` | Reads `RunTree`/`ToolCallSpan` — the data is already in the kit's shape. |
| LLM judge | kit `evals` | Goes through the kit's `ModelProvider`, so it is provider-agnostic by construction. |
| Metric definitions (cost + latency first class, kit issue #153) | kit `evals` | Must agree with `Usage`/`Cost` or the numbers won't reconcile. |
| Dataset **storage**, eval **run** orchestration as K8s Jobs, results in Postgres | **DevAI** | Needs the cluster, the registry and multi-tenancy. |
| Comparison, gates, promotion refusal, Kargo wiring | **DevAI** | Platform policy, not library concern. |
| Playground, trace viewer, compare UI | **DevAI** | |

This changes step 5–7 of the review's sequence from "build in DevAI" to "land in the kit,
consume in DevAI" — and it means DevAI issues #185/#186/#187 should be re-pointed at kit
issues #152–#155 rather than duplicating them.

## 5. Wiring order

Each step is independently shippable and leaves the tree working.

**W0 — Make it installable.** Bump `requires-python` to `>=3.12`; add
`tesserix-adk[redis,postgres]==0.1.1` sourced from the release assets; token in the
Dockerfile build stage and in CI; attestation verified. Resolve the `adk` name collision.
*Done when `import tesserix_adk` works in the built image and CI is green.*

Shipped: the name collision is resolved by `devai.kit` (consuming the kit) alongside the
existing `devai.adk` (authoring registry artifacts) — a smaller blast radius than renaming
the user-facing `devai adk` CLI. `devai.kit.versions` offers the five most recent releases
newest-first and defaults to the newest, `SandboxSpec.adk_version` pins the choice at
creation, `GET /api/adk/versions` feeds the picker, and the pod reads `DEVAI_ADK_VERSION`.
A GitHub outage degrades the *choice* to the release baked into the image, not the ability
to run.

**Still open in W0:** the image bakes exactly one kit release, so a sandbox that pins an
older one currently gets the baked one. Honouring the pin needs either per-release image
variants or an allowlisted package index reachable from the sandbox NetworkPolicy — decide
that with W3, when a pinned run first has something to lose by drifting.

**W1 — Adopt the trace spine (closes review Gap 3).** Map DevAI's run → `RunTree`;
persist `Node`/`Step`/`Totals` to `invocations` + `trace_spans`; feed the gateway's
`ToolCallRecord` in as `ToolCallSpan`; serve `GET /api/sandboxes/{id}/traces`. No agent
behaviour changes — this is capture only, and both producers already exist.

Shipped, ahead of the kit's own runner: `devai.sandbox.trace` carries the spine
(`TraceStep` kind = prompt | llm | tool | response, `Invocation.totals` derived from the
steps rather than counted separately), stored in Redis with the sandbox's own TTL — a
pinned configuration and its evidence expire together. Postgres persistence arrives with
W6, where an eval run outlives the sandbox that produced it.

**W2 — Anthropic provider.** Contribute it to the kit against
`ModelProviderConformance`, or bind DevAI's existing Anthropic adapter to the kit's
`ModelProvider` protocol. Without it the kit cannot run DevAI's primary model plane.
*Prerequisite for W3.*

**W3 — One run loop.** `AgentDispatcher`/`SpecAgent` build a kit `Agent` and delegate to
`AgentRunner`. DevAI keeps the resolvers (`PrincipalLLMResolver`, `deps.scm_for_principal`),
the tool gateway wraps the kit's `ToolRegistry`, and `Usage`/`Cost` flow into the existing
tables. This is the invasive one — it is also what makes every later step cheap.

**W4 — `POST /api/sandboxes/{id}/invoke` (closes Gap 2).** One turn through the kit
runner inside the pinned sandbox, returning a trace id. Playground and eval runner both
sit on it.

Shipped ahead of W3, on DevAI's own loop rather than the kit's: `SandboxInvoker` resolves
the pinned agent to a `Specialization`, forces the pinned model over the role's preference,
fences tool calls with the sandbox `ToolGateway` through `deps.extra["tool_dispatcher"]`,
and dispatches via `AgentDispatcher` so the run uses the invoking user's own credentials.
`POST /api/sandboxes/{id}/invoke` plus `GET .../traces[/{id}]` back the dashboard console.
When W3 lands, only `SandboxInvoker._run` changes — the boundaries and the trace stay put.

Gap 1 closed alongside it: `agent_envelope_to_spec` inverts `spec_to_agent_envelope`, and
`SpecializationService` adopts published agents disk has never heard of (local YAML wins
where both define a role, a malformed artifact is skipped rather than fatal). That is what
makes an agent authored in the UI runnable instead of merely listed.

Authoring closes the loop on top of it: `SandboxSpec.draft` pins an *unpublished* Agent
envelope, which `SandboxInvoker` maps with `agent_envelope_to_spec` instead of consulting
the catalog, so `/agents/studio` runs a definition before publishing it. The manifest
editor at `/agents/new` stays for people who want to write the CR directly.

The grading half landed with it. `sandbox/evals.py` runs a suite of saved inputs against a
sandbox and grades each one off the trace (text, tools called, token and latency budgets);
cases live in `spec.evals`, so they publish and version with the agent rather than sitting
in a side channel. Publishing over an existing name shows the spec diff and offers a new
version instead of a 409, and the studio can run one suite against two models on the same
draft. What W6 still owns is persistence beyond the sandbox TTL (Postgres, not Redis) and
promotion gates that read a suite result.

**W5 — Cassettes (closes Gap 5).** Replace the gateway's in-process `_recording` with a
kit `Cassette` stored as a versioned artifact; `RecordingProvider`/`ReplayingProvider`
make an eval suite deterministic across runs.

**W6 — Durable eval history and gates**, then the four UI tabs.

**W7 — Give back.** DevAI's MCP hub and A2A bus are production-proven and the kit's
`mcp`/`a2a` are empty. Upstream them rather than letting a second implementation grow.

Registry-as-runtime-source (review Gap 1) stays the parallel track: the kit gives an
`Agent` a typed, versioned definition, which is exactly the runnable artifact the registry
should be storing. **W3 and Gap 1 should be designed together** — doing W3 first and then
changing the definition shape would be two rewrites of the same code.

## 6. What to watch

- **Don't fork the kit inside DevAI.** If something is missing, the fix is a kit issue and
  a version bump, not a `devai/` copy that drifts.
- **0.1.x moves.** Pin exactly, read the changelog on every bump, and let the governed
  `api-surface.txt` diff be the review checklist.
- **Two loops is the failure mode.** W3 is the step that makes the integration real; the
  steps before it are safe but partial, and stopping there leaves the kit as decoration.
