# Autonomous SDLC — Gap Analysis & Implementation Plan

> **Status:** plan (2026-06-26). Built from a 4-subsystem code review (monitoring,
> scope/boardroom, loops/handover, memory). **Pending:** fold in the linked ChatGPT
> design notes (share link wasn't machine-readable — paste the content to integrate).

## The vision

DevAI should be a general **AI-harnessing platform for autonomous software delivery**:

> Connect & monitor a GitHub repo → when new backlog issue(s) appear, **auto-detect,
> read, and understand** them → **create multiple issues/stories if the scope needs it**
> → run a **proper boardroom discussion** → and **per the agreement, complete the full
> development** (implement → review → test → security → deploy) → deliver.

Usable for *any* such use case, not one bespoke flow.

## The end-to-end flow — and exactly where it breaks

| Vision step | Current state | Gap | Sev |
|---|---|---|---|
| **Connect + monitor a repo** | Onboarding marks a repo eligible; no issue-watch | No backlog watcher; webhooks not even auto-registered | **P0** |
| **Auto-detect new backlog issues** | Reactive webhook (`issues.opened/labeled`, `/devai` comment) or manual CLI/dashboard only | No poller; no "process-once" ledger → re-triggers | **P0** |
| **Understand the issue** | `requirements_analyst` (1 LLM call) → `analyzed_requirements[]` | Works once triggered. `category` is informational only | ok |
| **Scope → create multiple issues** | `create_stories` = single LLM prompt; **always 1 epic** | **Not scope-aware** — no sizing, no split decision, no multi-epic | **P1** |
| **Boardroom discussion** | Real multi-agent debate (bounded rounds, dissent) | Only runs if user ticks "Brainstorm"; **agreement overwritten** before code; **no implementer reads `boardroom_decision`** | **P0** |
| **Complete the FULL dev per agreement** | Single `implement-code` stage | **Implements only `stories[0]`** — multi-story is silently dropped (per-story loop exists only in the dead legacy orchestrator). Review/security verdicts **don't gate** | **P0** |
| **Deliver** | PR + CI ground-truth gating on build/tests | review/security advisory; no-CI repos can deploy failing tests | **P1** |

## Critical findings (the P0s that make the vision impossible today)

1. **Only `stories[0]` ever gets built.** `senior_developer` implements `stories[active_story_index]` (default 0, `agents/senior_developer.py:183-239`); the active `alm-pipeline.yaml` has one `implement-code` stage and nothing advances `active_story_index`. The per-story loop lives **only** in the superseded `graph/orchestrator.py`. → *Decomposition and implementation are disjoint; "complete the full dev" is structurally impossible for >1 story.*
2. **Review + security don't gate.** `review_decision` / `security_decision` are surfaced strings that **no stage consumes** to branch or halt; the stages are `gate: false` with no back-edge (`pipeline/stages/alm.py:276-307`, `blueprints/alm-pipeline.yaml:149-163`). A `changes_requested` review or a security `block` flows straight to deploy. The legacy hard-gate (`review↔implement` max 3, security→re-implement) was lost in the port.
3. **Concurrent same-repo runs collide.** No per-repo/per-issue lock; `senior_developer` mints a deterministic branch `story/{n}-{slug}` and `create_branch` adopts-on-422 → interleaved commits + duplicate PRs (`agents/senior_developer.py:254-259`, `pipeline/service.py:474-531`). No webhook `X-GitHub-Delivery` dedup.
4. **No autonomous backlog watcher.** Triggering is webhook-reactive or manual; the only periodic loop (`webhook/app.py:336 _reconcile_poller`) just probes the onboarding marker file — it never lists issues or dispatches (review #1).
5. **Boardroom agreement isn't binding.** The boardroom writes `technical_plan = decision`, but `create-plan` (EM) **overwrites** it (`agents/engineering_manager.py:198`), and the implementer prompt never injects `boardroom_decision` (`agents/senior_developer.py:342-363`). "Per the agreement" is cosmetic.

## Phased plan

Ordered so each phase makes the *next* vision capability real. Each item cites where it lands.

### Phase 0 — Make "complete the full dev" actually work *(unblocks everything)*
- **0.1 Multi-story implementation.** Fan implementation across all stories. Use the SDK's `RunContext.spawn` recursion (`agentruntime/agent.py:134-158`) for a per-story fan-out stage, **or** a blueprint story-loop in `blueprint/{planner,executor}.py`; set `active_story_index` in `build_alm_state`/`AgentStage`. *(P0)*
- **0.2 Make review + security gate.** Have `review-code`/`security-scan` emit a typed boolean verdict; add a bounded back-edge (re-implement on `changes_requested`/`block`, max N) **or** promote to `gate: true` with a `condition:` branch. Consume the verdict in the executor. *(P0)*
- **0.3 Per-repo run isolation.** Per-issue advisory lock at `dispatch()` + a run-unique branch suffix (`senior_developer.py:256`) + webhook delivery-id dedup. *(P0)*

### Phase 1 — Autonomous monitoring *(connect + auto-detect)*
- **1.1 Backlog watcher.** New periodic loop (model on `_reconcile_poller`): for each `ONBOARDED` repo, `scm.list_issues(state=open, labels=...)`, dispatch new ones. Gated by `DEVAI_ISSUE_WATCH_ENABLED`/`_INTERVAL`. New `src/devai/onboarding/watcher.py`, started in `webhook/app.py` lifespan. *(P0)*
- **1.2 Process-once ledger.** Record processed inbound issues (label/comment marker on the issue + a `processed_issues` table in tesserix-k8s schema), keyed on `updated_at` so edits can re-trigger. Prevents the poller re-running the same issue. *(P0)*
- **1.3 Per-repo watch config.** Add a `watch:` block (`enabled`, `labels`, `blueprint`) to the `.platform/devai.yaml` marker (`onboarding/models.py`, `marker.py`). *(P1)*
- **1.4 Concurrency guard.** Max-in-flight-per-repo + oldest-first in the watcher (reuse `services/guardrails.py RateLimiter`). *(P1)*
- **1.5 Auto-register webhooks** on connect (`scm/base.py` + GitHub impl, called from onboarding) for low-latency alongside the poll safety net. *(P2)*

### Phase 2 — Scope-aware understanding + binding boardroom + triage
- **2.1 Scope classifier.** Emit `scope_size` (small/med/large) + expected story-count band before `create_stories` (extend `requirements_analyst` or a thin stage); validate the LLM's story count against it; allow **multiple epics** for large asks (`product_director.py:211` hardcodes one). *(P1)*
- **2.2 Bind the boardroom.** Stop the EM clobber (extend, don't overwrite `technical_plan`); inject `boardroom_decision` into the implementer prompt + `DEFAULT_SURFACE_KEYS`; contract-test that the decision text reaches implement. *(P0)*
- **2.3 Default boardroom on for non-trivial scope** — change the `alm-pipeline.yaml:86` condition from `output.brainstorm` to `brainstorm or scope_large`. *(P1)*
- **2.4 Issue triage/router.** Classify issue content (feature/bug/docs/question/infra) → select blueprint; add lightweight `question`/`docs` blueprints (today everything → full `alm-pipeline`, `webhook/routes.py:560-569`). *(P2)*

### Phase 3 — Robust loops + handover + clarification
- **3.1 Convergent quality loops with escalation.** Bounded review/test/security loops that either converge or **escalate to a human gate** — never silently pass. Fix the one-shot test loop + the no-CI leak (gate on residual `test_failed`). *(P1)*
- **3.2 Mid-run "ask a human."** Wire the dead `AWAITING_HUMAN_INPUT`/`HUMAN_TAKEOVER` states + a clarification gate any stage can raise (the `dynamic_gates` mechanism, `lifecycle.py:446`, already supports it) so agents stop guessing on ambiguity. *(P1)*
- **3.3 Typed, richer handover.** A typed inter-stage contract beyond the 15-scalar whitelist (`agentruntime/agent.py:52-68`); make A2A escalations/handoffs **actionable** (consumed by control flow), not write-only telemetry (`graph/a2a.py:148-159`, dead `database.py:267`). *(P2)*

### Phase 4 — Memory that actually learns
- **4.1 Turn on semantic memory in prod.** `DEVAI_MEMORY_PROVIDER=pgvector` + `DEVAI_EMBEDDING_PROVIDER=openai` in tesserix-k8s prod values (today defaults to **redis = keyword-only**; `reinforce`/decay/dedup are inert). *(P1)*
- **4.2 Recall to the decision-makers.** Per-agent recall in `AgentStage`/`build_alm_state` keyed on `(agent, repo, query)` so `tech_detector`/`db_engineer`/`engineering_manager` — who make the learnable mistakes — actually see memory (today 8/14 agents get nothing; `lifecycle.py:335`). *(P1)*
- **4.3 Per-user/tenant memory scoping.** Add an owner/tenant tag to `remember` + OR-scoped filter, mirroring `PrincipalLLMResolver` (today `'global'` leaks across tenants). *(P1)*
- **4.4 Mid-run learning at correction points** + a closed-loop e2e test (seed memory → assert it changes a later run). *(P2)*

### Phase 5 — Durability / harness hardening *(the "better AI-harnessing tool")*
- Turn on **Temporal** (`DEVAI_WORKFLOW_PROVIDER=temporal`) for crash-safe per-stage resume and **NATS WorkQueue** dispatch (both built, off by default) — see the `nats-dispatch-workqueue` + Temporal plans.
- Lean on the **new Agent SDK/ADK** (`src/devai/agentruntime/`) + its **collaboration patterns** (`deliberation`/`mixture`/`distillation`) to power the review loop (deliberation) and the boardroom/parallel analysis (mixture) on one clean seam.

## Scenario coverage ("think of all scenarios")

| Scenario | Today | Fix (phase) |
|---|---|---|
| Multi-story / large multi-feature ask | ❌ only story[0] built | 0.1 |
| Review never approves | ❌ ships anyway | 0.2 |
| Security keeps blocking | ❌ ships anyway | 0.2 |
| Tests fail past max (CI-backed) | ✅ fails visibly (runbook) | — |
| Tests fail past max (no CI) | ❌ silently deploys | 3.1 |
| Stage hangs / times out | ✅ timeout→retry→heal→runbook | — |
| Pod dies mid-run | ✅ lease+reaper+snapshot resume | (tighten w/ Temporal, 5) |
| Ambiguous requirement mid-run | ⚠️ guesses (only pre-impl clarity check) | 3.2 |
| Human takeover / pause / resume | ⚠️ pause/resume ✅, `HUMAN_TAKEOVER` dead | 3.2 |
| Concurrent runs, same repo | ❌ branch collision / dup PR | 0.3 |
| Non-code issue (docs/question) | ❌ runs full code pipeline | 2.4 |
| New issues while busy (backlog burst) | ❌ no watcher / no cap | 1.1, 1.4 |
| Same issue seen twice | ❌ re-runs | 1.2, 0.3 |
| Repo conventions / past mistakes | ⚠️ captured, not recalled to maker | 4.1, 4.2 |
| Cross-tenant memory leak | ❌ `'global'` shared | 4.3 |

## What's already solid — preserve, don't disturb
Execution durability (lease + reaper + reconciler + snapshot/skip resume, progress-aware timeouts, retry→heal→runbook with fail-visible `on_failure: stop`); **CI ground-truth validators** (the one place agent narration can't lie); pause/stop/resume + approval gates with resumable timeouts; the **boardroom debate engine** itself; idempotent epic/story creation + GitHub tracked-issue linking; the **Agent SDK/ADK** + collaboration patterns just shipped; the adapter pattern throughout.

## Platform-maturity lens (from the linked AI-engineering framework)

The shared notes frame a mature AI platform as **systems engineering, not prompting** — "reliable, observable, secure, cost-effective, continuously improving even as models/tools/needs evolve." Its 15-layer architecture (Frontend → Gateway → Auth → **Policy Engine** → **Agent Runtime** → **Planner** → **Memory** → **Tool Registry** → **MCP Gateway** → Tools → **Observability** → **Knowledge Base** → **Model Router** → LLMs) maps almost 1:1 onto DevAI — which **validates the adapter/SDK direction**. Mapping each of the 15 areas to DevAI's actual state surfaces 4 *cross-cutting* workstreams the flow-analysis above didn't cover:

| Area | DevAI today | Verdict |
|---|---|---|
| 1 Product thinking (deterministic vs LLM) | triage gap — non-code issues run full LLM pipeline | → 2.4 |
| 2 Layered architecture | agentgateway / SDK-ADK / planner stages / memory+tools adapters / MCP hub / telemetry / LLM adapters | ✅ have it |
| 3 Model independence | LLM adapter family + InstrumentedLLMAdapter router + role chains + fallback | ✅ strong |
| **4 Evaluation** (correctness, hallucination, tool-success %, planning quality, **loop detection**, recovery rate) | **none** — boardroom + CI ground-truth are point-checks, no scoring pipeline | ❌ **NEW: Eval** |
| 5 Observability (per-step latency/tokens/cost/retries + trace/session/workflow IDs) | telemetry(otel) + run-event spine + LangSmith + usage ledger | ⚠️ extend (correlated per-step) |
| **6 Cost engineering** (semantic/response cache, model cascading, context compression) | usage ledger + role pricing + Anthropic prompt cache only | ⚠️ **NEW: Cost/Caching** |
| 7 Context engineering (relevance/ranking/dedup/permission-aware) | = the memory + handover gaps | → 4.x |
| 8 Memory (conversation/user-pref/team/workflow/scratchpad/org) | 3 types (episodic/semantic/procedural), keyword-only in prod | → 4.x (+ richer taxonomy) |
| 9 Tool ecosystem (versioning, idempotency, capability discovery, health) | tools/registry + MCP hub + unified ToolDispatcher | ⚠️ mostly have; add health/versioning |
| 10 Security (injection, exfil, tool abuse, secret leak, supply-chain) | security_expert + InputSanitizer + prompt_guard + git/path/url guards | ⚠️ have guards, but **gate doesn't gate** → 0.2 |
| 11 Human oversight (classify action risk → approval) | risk_level + approval gates | ⚠️ align; add action-class taxonomy → 3.2 |
| 12 Reliability (downtime, retries, fallbacks, circuit breakers, graceful degrade) | resume + retry→heal→runbook + CircuitBreaker + Noop degrade | ✅ strong (convergence loops weak → 0.2/3.1) |
| **13 Governance** (prompt/model/tool/policy version + who/why per action) | audit_log + boardroom decision + InstrumentedLLM | ⚠️ **NEW: Provenance** (prompt/model versioning + full decision trail) |
| **14 Developer experience** (replay, prompt versioning, regression tests, synthetic datasets, trace viz) | dashboard + LangSmith + authoring editor + sandboxctl | ⚠️ **NEW: DevEx** (Temporal gives replay) |
| 15 Continuous improvement (feedback/failures/success → refine) | = Eval(#4) + Memory loop | ❌ → Eval + 4.x |

### New cross-cutting workstreams (added to the plan)
- **Phase 6 — Evaluation & continuous improvement (HIGH, was missing).** An eval harness scoring every run/agent: correctness, groundedness/hallucination, tool-success %, planning quality, **infinite-loop / non-progress detection**, recovery rate; persist scores; feed them back to prompt/routing/tool refinement. This is the engine behind "continuously improving" and the objective gate the quality loops (0.2/3.1) need to escalate on. Natural home: a `telemetry`/`eval` adapter + an `alm_evaluate` stage + the analytics page. Use the SDK's **adversarial-verify** collaboration shape for LLM-judge scoring.
- **Phase 7 — Cost & caching.** Semantic + response caching (cache adapter family already planned), model **cascading** (cheap→escalate = the `distillation` pattern we shipped), context compression. Hooks: the `InstrumentedLLMAdapter` chokepoint + the cache adapter.
- **Phase 8 — Governance / provenance.** Stamp every action with prompt-version + model-version + tools + policy + boardroom-decision + approver + rationale; make `audit_log` the queryable provenance store. Extends the existing model-policy chokepoint.
- **Phase 9 — Developer experience.** Workflow **replay** (free once Temporal is on, Phase 5), prompt versioning, regression/eval suites + **synthetic datasets**, trace visualization. Ties Eval (6) to a repeatable test loop.

### Reconciled priority (flow gaps × platform maturity)
1. **P0 flow:** 0.1 multi-story, 0.2 real gating (security/review), 0.3 run isolation — *nothing ships correctly without these.*
2. **P0/P1 autonomy:** 1.1 backlog watcher + 1.2 dedup ledger (the "monitor & auto-detect" core), 2.2 bind the boardroom.
3. **P1 quality engine:** Phase 6 **Evaluation** (objective scores) + 3.1 convergent loops that escalate on those scores + 4.1/4.2 memory that recalls to the makers.
4. **P1 scope:** 2.1 scope-aware decomposition, 2.3 boardroom-on-by-scope.
5. **P2 maturity:** Cost/caching (7), governance/provenance (8), DevEx/replay (9), Temporal/NATS (5).

The framework's closing point is exactly the thesis for this whole plan: *"AI products are distributed systems with probabilistic components — the challenge is no longer generating good responses, it's building a platform that is reliable, observable, secure, cost-effective, and continuously improving."* DevAI already has the architecture and reliability; the gaps are **gating/convergence, autonomous triggering, scope-aware decomposition, evaluation, and a memory loop that actually closes.**
