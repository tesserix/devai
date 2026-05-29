# DevAI SDK + ADK — Implementation Plan

> **Goal:** one way to *build* an agent (SDK), one way to *run* it (ADK), and
> Kubernetes **Jobs** as the unit of execution so a trigger spins up
> ephemeral pods (one per agent run, many in parallel) instead of work
> happening inside a long-lived API pod.
>
> **Non-goal:** a heavyweight framework. We keep a thin layer over what
> already exists — `adapters/registry`, `runtime/job_spec`, `graph/a2a`,
> `pipeline/interfaces`. No new infra, no new vendor SDK in business logic.

---

## 1. Why — the problem in the code today

There are **three disjoint "agent" abstractions** and **two execution paths
that don't share code**:

| Abstraction | What it is | Has an execute method? |
|---|---|---|
| `core/base_agent.py::BaseAgent._execute_graph(state, a2a)` | the 20 real Python agents | yes (in-process only) |
| `specializations/base.py::Specialization` | a YAML role (prompt, tools, model) | **no — data only** |
| aregistry `Agent` profile | catalog record (image / skills / mcp_servers) | n/a (just metadata) |

Two paths consume them differently:

- **Inline path** — `graph/orchestrator.py::_run_node` imports the class by
  dotted path, constructs `cls(scm, state_manager, config)`, builds an
  `A2ABus`, calls `agent.run(state)`.
- **Job path** — `runner/entrypoint.py` reads `DEVAI_AGENT_PROFILE`, calls
  `SpecializationService.invoke(...)` **which does not exist**, catches the
  `AttributeError`, and falls back to reflection
  (`ClassName = CamelCase(agent_name) + "Agent"`, constructed with
  `(None, None, config, None)`).

So: MCP resolution lives only in the runner, A2A wiring is duplicated,
context is assembled two different ways, and the job path is held together
by `AttributeError` fallbacks. Adding an agent means touching `agents/`,
`ALL_AGENTS`, orchestrator node wiring, a YAML spec, and an aregistry seed.

**The fix is to collapse 3 abstractions → 1 contract (SDK) and 2 paths → 1
dispatcher with pluggable backends (ADK).**

---

## 2. The two concepts, defined

### SDK — *DevAI Agent SDK* — how you BUILD an agent

One contract. An author writes it once; it never knows or cares whether it
runs inline or in a Job.

```
src/devai/sdk/
  __init__.py      # re-exports: Agent, RunContext, AgentResult, agent, get_agent
  context.py       # RunContext  — everything an agent needs, assembled identically by every backend
  result.py        # AgentResult — what an agent returns
  agent.py         # Agent       — the ONE protocol every agent satisfies
  registry.py      # in-process name→Agent map + @agent("name") decorator
  legacy.py        # adapts existing BaseAgent subclasses to Agent (zero rewrite)
  spec_agent.py    # generic Agent that runs a YAML Specialization via the LLM adapter
```

**The contract (`agent.py`):**

```python
class Agent(Protocol):
    name: str
    async def run(self, ctx: RunContext) -> AgentResult: ...
```

**`RunContext` (`context.py`)** — built the SAME way by every backend, so an
agent behaves identically inline or in a Job:

```python
@dataclass(frozen=True, slots=True)
class RunContext:
    task: DevAITask              # intent, repo, blueprint, agent_context (handover bag)
    profile: AgentProfile        # from aregistry: model, skills, prompts, mcp_servers
    identity: Identity           # triggered_by, trace_id, principal
    deps: StageDeps              # scm, llm, memory, event_bus — all adapters (already exist)
    mcp: list[McpEndpoint]       # resolved, gateway-aware endpoints (built ONCE, see §4)
    a2a: A2ABus                  # pre-wired with identity (graph/a2a.py)
    config: Settings
```

**`AgentResult` (`result.py`):**

```python
@dataclass(slots=True)
class AgentResult:
    ok: bool
    data: dict[str, Any]              # handover → merged into task.agent_context
    a2a_messages: list[dict] = ...    # outbox, persisted by the dispatcher
    error: str = ""
```

**No rewrites.** Two adapters bridge the existing world into the contract:

- `legacy.py::LegacyAgent` wraps a `BaseAgent` subclass: builds the `ALMState`
  slice from `ctx`, calls the existing `agent.run(state)`, maps the partial
  state back to `AgentResult`. → the 20 existing agents satisfy `Agent` for
  free.
- `spec_agent.py::SpecAgent` runs a YAML-only `Specialization` (prompter,
  intake, negotiator, …) directly against `ctx.deps.llm` with `profile.skills`
  as tools. → this is the `SpecializationService.invoke()` that the runner is
  already trying to call, finally implemented in one place.

### ADK — *DevAI Agent Dispatch Kit* — how an agent RUNS

One entry point. It resolves the profile, resolves MCP, picks a backend,
runs, and persists — all in one place.

```
src/devai/adk/
  __init__.py
  dispatcher.py        # AgentDispatcher.dispatch() / dispatch_many()  ← the ONE entry
  profile.py           # AgentProfile + resolve_profile(registry, name)   (from aregistry)
  mcp.py               # resolve_mcp_endpoints(profile, registry, config)  (moved out of runner)
  backends/
    base.py            # ExecutionBackend ABC: async run(agent_name, ctx) -> AgentResult
    inline.py          # InlineBackend  — await get_agent(name).run(ctx)   (API pod, dev/fallback)
    job.py             # JobBackend     — build_job_spec + submit + watch + parse RESULT
    # kagent.py        — FUTURE optional backend; same ABC (see §7)
```

**The dispatcher (`dispatcher.py`):**

```python
class AgentDispatcher:
    def __init__(self, deps: StageDeps, backend: ExecutionBackend, registry): ...

    async def dispatch(self, agent_name: str, task: DevAITask,
                       *, stage_config: dict | None = None) -> AgentResult:
        profile  = resolve_profile(self.registry, agent_name)        # aregistry, ONCE
        mcp      = resolve_mcp_endpoints(profile, self.registry, self.cfg)  # gateway-aware, ONCE
        ctx      = build_run_context(task, profile, mcp, self.deps, stage_config)
        result   = await self.backend.run(agent_name, ctx)           # inline OR job
        await self._persist_a2a(task, result.a2a_messages)           # ONCE
        return result

    async def dispatch_many(self, calls: list[Call]) -> list[AgentResult]:
        return await asyncio.gather(*(self.dispatch(c.agent, c.task, ...) for c in calls))
```

`build_run_context` is **the single shared helper** used by the dispatcher
(for InlineBackend) AND by `runner/entrypoint.py` (inside the Job). That is
what guarantees inline and job runs are byte-for-byte equivalent.

---

## 3. Jobs as the execution unit (the deployment shape)

```
                         ┌──────────────── CONTROL PLANE (long-lived) ────────────────┐
  webhook / cron / CLI → │  devai-api pod                                              │
                         │   • LangGraph workflow (graph/orchestrator.py) decides WHAT │
                         │   • AgentDispatcher decides HOW + WHERE                      │
                         │   • JobWatcher watches Jobs, persists state + A2A           │
                         └───────────────┬─────────────────────────────────────────────┘
                                         │ dispatch() / dispatch_many()
                                         ▼  (one K8s Job per agent run)
                         ┌──────────── DATA PLANE (ephemeral Jobs) ───────────────────┐
                         │  devai-runner Job   devai-runner Job   devai-runner Job ... │
                         │  (senior_developer) (db_engineer)      (qa_tester)          │
                         │   runner/entrypoint.py → build_run_context → agent.run(ctx) │
                         │   emits RESULT:: on stdout, then the pod exits              │
                         └─────────────────────────────────────────────────────────────┘
```

- **The API pod is control plane only** — it never runs LLM work itself once
  `k8s_runtime_enabled=true`. It decides, dispatches, watches, persists.
- **Each agent run is one Job → one pod**, runs to completion, then exits.
  `runtime/job_spec.py` already renders this; `runtime/job_watcher.py` already
  watches it. We reuse both unchanged.
- **"Spin up multiple pods" = `dispatch_many()`** fanning out N Jobs:
  - **ALM per-story loop** — stories are independent (own branch, own PR), so
    `implement → db → review → security → test` for story A runs as a Job
    chain concurrently with story B. Today the orchestrator loops
    sequentially; `dispatch_many` over `story_branches` makes it parallel pods.
  - **SRE monitors** — `infra / log / perf / cost / capacity` already run via
    `asyncio.gather` in-process; they become 5 parallel monitor Jobs.

LangGraph stays as the **workflow engine** (gates, loops, conditional edges).
It just delegates each node's *execution* to `dispatcher.dispatch(...)`
instead of constructing the agent inline.

---

## 4. What gets deleted / moved (the cleanup)

| Today | After |
|---|---|
| `runner/entrypoint.py` `_run_agent` + `_invoke_legacy` reflection + `AttributeError` fallback | thin: `ctx = build_run_context(env); result = await get_agent(name).run(ctx); emit(result)` |
| `runner/entrypoint.py::_resolve_mcp_endpoints` | moved to `adk/mcp.py`, used by both runner and dispatcher |
| `orchestrator.py::_run_node` dotted-path import + `cls(scm, state, config)` + per-node A2ABus | `await dispatcher.dispatch(node_agent_name, task)` |
| `pipeline/stages/job_runner.py::JobRunnerStage` (does profile fetch + image pick + submit + watch + parse) | becomes a thin caller of `JobBackend`; profile/MCP logic moves into the ADK |
| `SpecializationService.invoke()` (referenced, missing) | implemented once as `SpecAgent` in the SDK |
| 3 agent abstractions | 1 `Agent` contract; `Specialization`/`BaseAgent`/profile become *inputs* to it |

Net new code is ~8 small files; the win is deleting duplicated context/MCP/A2A
wiring and the reflection fallback.

---

## 5. Phased rollout (each phase ships green, nothing big-bang)

**Phase 0 — contracts (no behavior change).**
Add `src/devai/sdk/` (`Agent`, `RunContext`, `AgentResult`, registry,
`LegacyAgent`, `SpecAgent`). Unit tests: a `LegacyAgent` wrapping
`SeniorDeveloperAgent` and a `SpecAgent` running a YAML-only spec both return
a valid `AgentResult`. Nothing wired yet.

**Phase 1 — the ADK with InlineBackend.**
Add `src/devai/adk/` with `AgentDispatcher` + `InlineBackend` + `profile.py` +
`mcp.py` (moved from the runner). Point `orchestrator._run_node` at
`dispatcher.dispatch(...)` with `InlineBackend`. Behavior identical to today,
but now there is ONE path that builds context, resolves MCP, wires A2A.
Contract tests: dispatch every ALM agent inline, assert outputs unchanged.

**Phase 2 — JobBackend + thin runner.**
Implement `JobBackend` (wraps `runtime/job_spec` + `job_watcher`). Rewrite
`runner/entrypoint.py` to the 3-line `build_run_context → run → emit`. Refactor
`JobRunnerStage` to delegate to `JobBackend`. Flip `k8s_runtime_enabled=true`
in a dev cluster (sandboxctl) and run one agent as a Job. **Acceptance: the
same agent produces the same `AgentResult` inline and as a Job** (this is the
proof the SDK seam is real — mirrors the adapter "contract test" rule).

**Phase 3 — fan-out (multiple pods).**
Add `dispatch_many`. Parallelize the ALM per-story loop (one Job chain per
story) and the SRE monitors (5 monitor Jobs). Cap concurrency via a semaphore
(reuse the onboarding `max 8` pattern). Dashboard shows N concurrent runner
pods on the timeline.

**Phase 4 — make Jobs the default in prod.**
`tesserix-k8s` chart: `DEVAI_K8S_RUNTIME_ENABLED=true`, RBAC for the API SA to
create/watch Jobs in `devai`, `devai-runner` image in CI. API pod becomes pure
control plane. InlineBackend stays as the local-dev / graceful-degrade path
(same rule as Noop adapters: never hard-fail, fall back to inline).

---

## 6. How it stays reusable + connected to everything

- **Adapters unchanged.** `RunContext.deps` is the existing `StageDeps` —
  agents talk to memory / llm / scm / event_bus through the ABCs that already
  exist. Swap a backend with one env var, exactly as the adapter pattern
  mandates.
- **aregistry is the single source of agent truth.** `resolve_profile` is the
  one place that reads it; the profile is baked into `DEVAI_AGENT_PROFILE` so
  the Job doesn't re-query (already the design in `AGENTIC-INTEGRATION.md`).
- **MCP in one place.** `adk/mcp.py::resolve_mcp_endpoints` — gateway-aware,
  used by both the dispatcher and the runner. Add an MCP server in aregistry →
  every agent can reach it, no code change.
- **A2A + identity in one place.** The dispatcher builds the `A2ABus` with
  `triggered_by`/`trace_id` and persists the outbox once. Agents just call
  `ctx.a2a.handoff(...)` — identity is inherited, never re-stamped.
- **Adding an agent becomes one step:** write a YAML spec (or a class) +
  register a name. No orchestrator edits, no `ALL_AGENTS`, no reflection.

---

## 7. Deliberately deferred (don't over-build now)

- **kagent as a backend.** It's deployed but DevAI dispatches Jobs directly to
  the K8s API today. When we want lifecycle/queueing/retries managed for us, add
  `backends/kagent.py` implementing the same `ExecutionBackend` ABC — no change
  to the SDK or the dispatcher. Until then, JobBackend (direct K8s) is enough.
- **agentgateway MCP backend rules.** `resolve_mcp_endpoints` already emits the
  gateway URL when `DEVAI_AGENTGATEWAY_URL` is set; wiring solo.io's per-server
  routing in `tesserix-k8s/charts/apps/agentgateway` is independent infra work.
- **No third-party agent framework** (Google ADK / LangChain AgentExecutor as
  the core). We keep our thin `Agent`/`RunContext` contract so we own the seam
  and stay multi-provider via our own adapters. We can *wrap* such a framework
  inside a single `Agent` later if ever needed.

---

## 8. North star — zero-code custom agents (UI → YAML → run)

The reason the SDK/ADK split exists: **a user creates an agent in the UI and
it runs seamlessly with no code, no redeploy, no orchestrator edit.** A custom
agent is *data*, not a class.

**A custom agent = two registry writes:**

```
UI "New Agent"  →  YAML spec (prompt, model, allowed_tools, handover_schema, risk_level)
                →  published to aregistry as an agent record (name, model, mcp_servers)
                →  dispatched by SpecAgent — the generic Agent that runs ANY YAML spec

UI "New Tool"   →  register an MCP server (name + endpoint/spec) in the registry
                →  referenced by name under the agent's `mcp_servers`
                →  reached at runtime by adk/mcp.py (gateway-aware), same for every agent
```

**Why no code is needed:** the ADK never imports a per-agent Python class. It
looks the name up in the SDK registry; a YAML-only agent resolves to
`SpecAgent`, which drives `ctx.deps.llm` using the spec's prompt + the tools
its `mcp_servers` expose. The dispatcher gives it the *same* Job execution,
MCP resolution, A2A wiring, and identity propagation as a built-in agent.

**The key design rule — custom tools are MCP servers, never user Python.**
Running arbitrary user-authored tool code in-process would break the
"YAML-only, no redeploy" promise and is a security hole. So "create a tool" in
the UI = register an MCP server record (name + URL). The tool runs as its own
sandboxed process/pod; DevAI talks to it as an MCP client and never imports it.
A curated *built-in* tool (non-MCP) is allowed only from a vetted catalog the
YAML selects by name.

**What this adds (small — most falls out of the existing design):**
- **Validate on publish** — `specializations/validator.py` runs on the UI's
  "save agent" path; bad YAML is rejected before it reaches the registry.
- **Tool allowlist + risk gate** — a user agent may only reference *registered*
  MCP servers; `RiskLevel.HIGH/CRITICAL` specs park in `awaiting_approval`
  (the `needs_human_gate` hook already exists).
- **Tenant scoping** — custom agents and MCP servers carry their owner so one
  tenant cannot dispatch another tenant's tools.

This is a Phase-3/4 capability, but the contracts in Phases 0–1 must already
treat YAML-only agents as first-class (i.e. `SpecAgent` is built and tested in
Phase 0, not bolted on later).

---

## 9. File-level checklist

```
NEW   src/devai/sdk/{__init__,agent,context,result,registry,legacy,spec_agent}.py
NEW   src/devai/adk/{__init__,dispatcher,profile,mcp}.py
NEW   src/devai/adk/backends/{base,inline,job}.py
EDIT  src/devai/graph/orchestrator.py        # _run_node → dispatcher.dispatch
EDIT  src/devai/runner/entrypoint.py         # thin: build_run_context → run → emit
EDIT  src/devai/pipeline/stages/job_runner.py# delegate to JobBackend
EDIT  src/devai/specializations/service.py   # add invoke() → delegates to SpecAgent (or drop, ADK owns it)
REUSE src/devai/runtime/{job_spec,job_watcher,k8s_client}.py   # unchanged
REUSE src/devai/adapters/registry/*          # profile source
REUSE src/devai/graph/a2a.py                 # A2A bus
NEW   tests/unit/test_sdk_agent.py           # LegacyAgent + SpecAgent → AgentResult
NEW   tests/unit/test_adk_dispatch.py        # inline == job equivalence (the key test)
```

---

### One-paragraph summary

**SDK** = one `Agent` contract (`run(ctx) -> AgentResult`) that the existing 20
Python agents and the YAML specs both satisfy via thin adapters — write an
agent once. **ADK** = one `AgentDispatcher` that resolves the aregistry
profile, resolves MCP (gateway-aware), wires A2A + identity, and runs the agent
through a pluggable backend — `InlineBackend` for dev/fallback, `JobBackend`
for production where **every agent run is a Kubernetes Job (one pod), and
`dispatch_many` fans out into many pods** for parallel stories and SRE
monitors. The API pod becomes pure control plane. We reuse the adapter
pattern, `runtime/job_spec`, and `graph/a2a` wholesale, delete the duplicated
context/MCP/reflection code, and defer kagent to an optional future backend.
