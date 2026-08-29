# Agent Harness — build → test → security → publish gate for custom agents

**Status:** Static build/security/risk and durable evaluation gates implemented — 2026-08-20
**Goal:** when a user authors a new agent (dashboard ArtifactEditor or `devai adk`),
it must pass a real gate — **build** (refs resolve), **test** (eval dry-run),
**security** (injection + tool/MCP grants + risk) — before it can be **published**
to aregistry. The ALM pipeline already harnesses the *code agents write*; this
harnesses the *agent artifact itself*, which today nothing does.

## Delivered implementation

The authenticated `POST /api/registry/agents` boundary now owns the harness so
Agent Studio, the ADK CLI, and direct authenticated API clients cannot disagree
about the decision:

1. **Build** resolves `skill`, `skills`, `promptRef`, `prompts`, `tools`, and
   `mcpServers` against the caller-visible registry view. Malformed lists,
   dangling references, private cross-user references, and known
   provider/model mismatches fail closed.
2. **Test** is the durable sandbox evaluation gate from ADR 0003. When an Agent
   declares `spec.evalSuite`, the exact owner-scoped run, immutable suite and
   dataset versions, thresholds, and published baseline are verified before
   publication.
3. **Security** rejects instruction-override/secret-disclosure prompts and
   wildcard tool or MCP grants. High and critical risk levels require an
   authenticated admin approval reason and a successful append-only audit
   write. Static security failures cannot be overridden.
4. **Publish** stamps server-owned build, security, evaluation, and lifecycle
   evidence. Client-supplied evidence is discarded.

Agent Studio renders actionable static and evaluation failures and offers the
approval action only for evaluation failures or high/critical risk holds. A
published label is not runtime evidence: live `running` state comes from the
Substrate status work in #77. User-authored `kagent` selection remains
fail-closed until the isolation gate in #76 passes.

The original design below is retained as the implementation history. The main
change is intentional: the harness runs at the trusted server boundary rather
than only in `adk/publisher.py`, because a client-side-only gate is bypassable.

---

## Current state (audit)

The author→publish path exists, but the only gate is schema lint:

| Step | Where | What it actually checks |
|---|---|---|
| Author (UI) | `dashboard/src/components/artifact-editor.tsx`, `dashboard/src/lib/registry-schemas.ts` | form + live YAML; `lintManifest()` — structure, required fields, name format, size, `<script>`/`javascript:` content |
| Author (CLI/Py) | `src/devai/adk/scaffold.py`, `adk/builders.py` | write a starter YAML / fluent builder |
| Validate | `devai adk validate` (`src/devai/cli/adk_commands.py`), `specializations/validator.py`, `specializations/loader.py` | YAML schema, handover field **types**, allowed-tool catalog existence |
| Publish | `devai adk publish` → `adk/publisher.py::Publisher` → `registry/client.py::publish_agent` → `registry/routes.py:190-256` | tenant stamp, name uniqueness (409 + `?overwrite=true`), POST |

Gaps this plan closes:

1. **No build** — nothing confirms every referenced `skill` / `promptRef` /
   `mcp_servers` / `tools` actually **resolves** in the registry and is visible to
   the author's tenant. A dangling ref publishes fine and fails only at run time.
2. **No test** — the agent is never *executed* before publish. Evals
   (`docs/plans/analytics-cost-evals` Phase 3) are captured **from live runs**, not
   as a pre-publish gate. So a broken prompt / wrong handover shape ships.
3. **No security** — no prompt-injection scan, no check that referenced MCP servers
   are registered **and tenant-owned** (cross-tenant tool theft), no flag for tool
   grants beyond the declared category.
4. **Risk gate is in the wrong place** — `RiskLevel.needs_human_gate`
   (`specializations/base.py:79`) parks a run in `awaiting_approval` at *execution*
   (`pipeline/stages/specialization.py:472-481`). A `high`/`critical` agent should be
   gated at *publish*, before it ever exists in the catalog.

---

## Design

One new component — `src/devai/adk/harness.py::AgentHarness` — that every publish
path calls **before** the POST. Mirrors the ALM pipeline shape, but the "artifact"
is the agent spec, not application code.

```
AgentHarness.run(spec) -> HarnessReport
  build    →  test(eval)  →  security  →  (publish proceeds only if all green)
```

```python
@dataclass(slots=True)
class StageOutcome:
    stage: str                 # build | test | security
    ok: bool
    score: float = 1.0         # 0..1 (test stage); 1.0 for pass/fail stages
    findings: list[str] = ...  # human-readable, surfaced in UI + CLI
    gate: str = ""             # "" | "awaiting_approval" (risk gate)

@dataclass(slots=True)
class HarnessReport:
    ok: bool                   # all stages ok and no blocking gate
    outcomes: list[StageOutcome]
    requires_approval: bool    # high/critical risk → park, don't reject
```

### Stage 1 — build (static, no execution)
Resolve the agent's whole reference graph against the registry (tenant-scoped):
- every `skill`, `promptRef`, `mcp_servers[*]`, `tools[*]` exists and is visible to
  the author's tenant (reuse `registry/client.py::artifact_exists` + a `get_*`);
- `llm.provider` / `llm.model` are a known pair (reuse the model-policy chokepoint —
  `InstrumentedLLMAdapter` provider/model fit, see `[[project_model_policy_boardroom]]`);
- `limits` sane (`maxTurns`, `timeoutSeconds` within platform caps);
- `runtime.kind` is `python_class` (class importable) or `yaml-only`.
- Fail **closed** on any unresolved ref. This is the single biggest source of
  silent run-time breakage today.

### Stage 2 — test / eval (dry-run)
Execute the agent once per fixture in a **sandbox** `StageDeps` (Noop adapters +
a recorded/mock LLM so no real spend, no real SCM writes):
- assert the handover output satisfies `handover_schema` — reuse
  `specializations/validator.py::validate_handover(strict=True, require_non_empty=True)`;
- assert no tool call outside `allowed_tools`; assert `maxTurns`/`timeout` respected;
- score 0..1 (fraction of fixtures green) → persist to the `agent_evals` table from
  `docs/plans/analytics-cost-evals` Phase 3 (`evaluator="harness"`). Sub-threshold
  score blocks publish.
- **Dependency:** running a *yaml-only* agent needs a minimal generic runner
  (`SpecAgent`) — the smallest slice of `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md`.
  `python_class` agents can be exercised via the existing `runner/entrypoint.py`
  path. This is why the eval stage is **Phase 2** below, not Phase 1.

### Stage 3 — security (static + policy)
- **Prompt-injection scan** of `system_prompt` / `user_prompt_template` — known
  sinks ("ignore previous", tool-exfil patterns, secret-echo, URL-callback). Reuse
  the `security_expert` heuristics rather than inventing a second scanner.
- **MCP/tool ownership** — every `mcp_servers[*]` is registered **and** tenant-owned
  or public; reject a private cross-tenant reference (the `[[project_aregistry_tenancy]]`
  boundary).
- **Over-broad grant** — flag `allowed_tools` outside the agent's declared
  `category` / `roleColor`.
- **Risk gate at publish** — `RiskLevel.HIGH/CRITICAL` (`base.py:79`) →
  `requires_approval=True`; the artifact is parked `awaiting_approval` (reuse the
  existing `needs_human_gate` hook) instead of going live. Not a rejection — a hold.

### Wiring (one gate, all entry points call it)
- `adk/publisher.py::Publisher.publish` runs `AgentHarness.run` first; on `not ok`
  returns `PublishResult(status="failed", error=<report>)`; on `requires_approval`
  publishes with `metadata.lifecycle: awaiting_approval`.
- `devai adk publish` prints the per-stage report; `devai adk validate` gains a
  `--harness` flag to run the full gate without publishing (the "dry run the gate").
- Dashboard publish route (`registry/routes.py`) runs the harness and returns the
  `HarnessReport`; `ArtifactEditor` renders a stage panel (build/test/security =
  green/amber/red) and only enables **Publish** when green (amber = approval hold).
- **Break-glass:** `--skip-harness` / an operator-only `?skip_harness=true`, **always
  logged to `audit_log`**. Same "never hard-fail the platform" spirit as Noop
  adapters — but skipping is auditable, not silent.

---

## Phased rollout (each ships green)

**Phase 0 — skeleton + seam.** `adk/harness.py` with `HarnessReport`/`StageOutcome`
and a pass-through `AgentHarness` (returns green). Wire `Publisher`, the CLI, and the
publish route to call it. No behavior change; proves the seam. Unit test: publish
still works, report is attached.

**Phase 1 — build + security + risk-gate (static, no execution).** Implement Stage 1
and Stage 3. This is most of the value and needs **no** agent execution / SpecAgent.
Acceptance: a dangling `mcp_servers` ref, a cross-tenant MCP, and a `critical` risk
agent are each caught at publish (reject / reject / hold). Unit + a publish-route
integration test.

**Phase 2 — test/eval dry-run.** Build the minimal `SpecAgent` yaml-runner (smallest
slice of the SDK/ADK plan) + fixture harness + `agent_evals` write. Acceptance: an
agent whose prompt yields the wrong handover shape scores < threshold and is blocked;
a good one scores 1.0 and publishes. Reuses `validate_handover` for the assertion.

**Phase 3 — dashboard panel + break-glass + audit.** Stage panel in `ArtifactEditor`;
`--skip-harness` with `audit_log` entry; `awaiting_approval` artifacts surface in an
approvals view for an operator to release.

---

## Reuse map (almost nothing is new infra)

| Need | Reuse |
|---|---|
| handover assertion | `specializations/validator.py::validate_handover` |
| risk gate | `specializations/base.py::RiskLevel.needs_human_gate` |
| ref resolution + publish | `registry/client.py` (`artifact_exists`, `publish_agent`) |
| sandbox execution | Noop adapters (`adapters/*/noop.py`) + mock LLM |
| eval persistence | `agent_evals` schema (`docs/plans/analytics-cost-evals` Phase 3) |
| model/provider fit | `InstrumentedLLMAdapter` chokepoint ([[project_model_policy_boardroom]]) |
| security heuristics | `security_expert` agent |
| tenant boundary | aregistry tenancy ([[project_aregistry_tenancy]]) |

## Test + deploy
- Unit: each stage in isolation (resolver, injection scan, ownership, risk gate,
  eval scoring). Integration: publish route returns a red report and refuses; a
  `critical` agent parks `awaiting_approval`.
- `agent_evals` schema lands via `tesserix-k8s` db-schema-bootstrap (per CLAUDE.md
  rule 5 — no SQL in this repo). Code deploys via CI → ArgoCD.
