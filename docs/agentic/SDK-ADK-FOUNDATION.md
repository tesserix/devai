# Agent SDK + ADK — the unified runtime seam

> Supersedes the file layout in `IMPLEMENTATION-PLAN-SDK-ADK.md`. That plan
> reserved `src/devai/sdk/` and `src/devai/adk/` for the runtime seam, but those
> directories were filled with unrelated code (an HTTP client and an authoring
> toolkit). The real runtime seam lives in **`src/devai/agentruntime/`**, which
> already held the closest thing (`AgentRunner`).

## Why

DevAI had **three** disjoint "agent" abstractions, resolved by reflection in
**three** places with **three** constructor signatures, plus **two** divergent
YAML tool-loops. Adding one agent touched 5–7 files. The SDK/ADK collapses this
to **one contract** and **one dispatcher** so every agent — existing Python,
YAML-only, or future native — is defined once and run the same way everywhere
(inline, as a Job, or recursively).

## The two layers

### SDK — *what an agent is* (`agentruntime/agent.py`, `legacy.py`, `spec_agent.py`, `runner.py`)

```python
class Agent(Protocol):
    name: str
    async def run(self, ctx: RunContext) -> AgentResult: ...
```

- **`RunContext`** — the task + per-principal-resolved capabilities (LLM, SCM,
  Settings overlay) + the shared `StageDeps`. Built **once** by the dispatcher,
  so inline and Job runs see identical inputs. Carries `ctx.spawn(sub_agent)` for
  recursive decomposition.
- **`AgentResult`** — the typed result that replaces the bare `dict` patch.
  `to_stage_result(task)` nests the handover under the role key, surfaces the
  well-known scalars (`pr_number`, `branch_name`, …), re-surfaces A2A traffic,
  and mirrors structural fields onto the task — the boundary logic, in one place.
- **`LegacyAgent`** — adapts an existing `BaseAgent` (instance, class, or dotted
  path) to the protocol. **Replaces the three reflection bridges** with one. Maps
  `RunContext → ALMState` via the shared `build_alm_state`, calls
  `agent.run(state)`, wraps the patch via the shared `alm_patch_to_result`.
- **`SpecAgent`** — runs a YAML `Specialization`. YAML-only → the canonical
  `AgentRunner` tool-loop; `legacy_python_class` set → routes through
  `LegacyAgent`. **This is the long-missing `SpecializationService.invoke()`.**

### ADK — *how an agent runs* (`agentruntime/dispatch.py`)

- **`AgentDispatcher`** — `build_context()` is the single per-principal resolution
  point (the logic currently duplicated in `AgentAdapter.execute`,
  `AgentRunner.run`, and `_run_yaml_runner._select_llm`). `dispatch()` runs one
  agent; `dispatch_many()` fans out (the *mixture* pattern); the dispatcher
  attaches itself to every context so `ctx.spawn()` recurses.
- **`ExecutionBackend`** — `InlineBackend` (default + graceful-degrade fallback)
  now; **`JobBackend` later** reuses `runtime/job_spec.py` + `job_watcher.py`, so
  inline↔Job becomes a config swap, not a separate code path.

## Recursion / collaboration

`ctx.spawn()` gives ROMA / RecursiveMAS-style decomposition for free: a root
agent splits its task and delegates to sub-agents on one dispatcher. The four
RecursiveMAS collaboration patterns map onto this seam as named helpers (Phase
5): *deliberation* = a review loop, *mixture* = `dispatch_many` + aggregate,
*distillation* = cheap-model-first → escalate, *sequential* = the chain itself.
(Note: RecursiveMAS's *latent-state* mechanism needs white-box GPU models and
does not apply to DevAI's API-based plane — only the orchestration ideology
transfers. See the `project_sdk_adk` memory note.)

## Phasing

| Phase | Scope | Status |
|---|---|---|
| **1** | Additive foundation: `Agent`/`RunContext`/`AgentResult`, `LegacyAgent`, `SpecAgent`, `AgentDispatcher`/`InlineBackend`, contract tests. **Wires nothing into live paths.** | **Shipped** — `tests/unit/test_agentruntime_sdk.py` (10 tests) |
| **2** | Generic `AgentStage` over the dispatcher; the per-principal + trial-gate preamble extracted from `AgentAdapter.execute` into the shared `pipeline/principal.py`; dispatcher gains config/scm overrides. | **Shipped** |
| **3a** | Migrate **all 14** `AgentAdapter` subclasses (`stages/alm.py`) to data-driven `AgentStage` factories + standalone output validators. Registry unchanged (same factory names). `AgentAdapter` now has zero code references. | **Shipped** — full unit suite green (920 passed) |
| **3b-i** | `run_specialization`'s legacy bridge now runs via `LegacyAgent` (reflection removed; behavior-identical — explicit `deps.config`/`deps.scm` overrides). `_build_alm_state` + `import importlib` deleted. | **Shipped** — 42 specialization tests + full suite green (920) |
| **3b-ii** | **Tool *execution* unified on `ToolDispatcher`** — `AgentRunner` now resolves+executes via `ToolDispatcher` (gaining the validation/security/test/memory/SRE families + dry-run gating), passing a rich `ToolContext` so registry-backed tools (shell/web/checkpoint/gitops) keep workdir/web_search; `scm`+`file` tools dropped from `ToolDispatcher`'s `_index` so they route through the registry's *rich* `SCMToolExecutor` (run_id/redis/audit) instead of a bare one. Both runners now share one executor. `registry.bind` is retained (still used by `mcphub/tool_server.py`). | **Shipped** — full suite green (920) |
| **3b-iii** | **Duplicate runner loop deleted.** `run_specialization` YAML now routes through `SpecAgent`→`AgentRunner` (the same loop crew uses); `_run_yaml_runner`/`_select_llm`/`_render_skill_guidance`/`_build_user_prompt`/`_parse_handover` removed. Ported `_select_llm`→`SpecAgent._resolve_spec_llm` + skill-profile→`SpecAgent._skill_guidance`; `AgentRunner.run` gained `llm`-override, `system_suffix`, per-request telemetry `extra`, and honors `extra["tool_dispatcher"]`; canonical no-JSON handover fallback is `{"text":…}` (doesn't fake a required `summary`). **`AgentRunner` is now the single LLM+tool loop.** | **Shipped** — full suite green (920) |
| **3d** | **`AgentAdapter` + `_safe_agent` + construction contextvars deleted** from `stages/_base.py` (only `run_correlation_label` remains; ~340 → 27 lines). The 2 fixtures (`test_settings_llm_resolver`, `test_run_event_spine`) migrated to test the new homes (`resolve_principal_run`, `alm_patch_to_result().to_stage_result()`). | **Shipped** — full suite green (920) |
| **3c-entrypoint** | **`SpecializationService.invoke()` implemented on the SDK.** The Job runner's primary path now runs YAML specs via `SpecAgent` → `AgentRunner` (so a YAML role behaves identically as a Job or inline — **closes the gap where YAML-only roles couldn't run as Jobs at all**). `invoke()` returns `None` for a legacy/unknown agent → the entrypoint's reflection is now a genuine *last-resort fallback* for legacy Python classes, not the primary path. | **Shipped** — `tests/unit/test_specialization_invoke.py` (4 tests), suite green (924) |
| **3c-residual** | The two reflection sites that remain are both genuine cold-path fallbacks: `orchestrator._run_node` (LangGraph fallback, rarely hit) and `entrypoint._invoke_legacy` (legacy Python classes in a Job — kept because `LegacyAgent`'s deps-guard requires a `state_manager` the Job pod lacks; the clean removal belongs with enabling the Job runtime so it's testable). | Deferred |
| **4** | `JobBackend` (reuse `runtime/`); fix the Job path's missing `invoke()` (now `SpecAgent`); inline≡Job equivalence test. | Pending |
| **5** | Recursion helpers + named collaboration patterns. | Pending |

## Design rules

1. **One contract.** Nothing calls `agent.run` directly except the SDK adapters;
   everything else goes through `AgentDispatcher`.
2. **Never hard-fail.** Construction/import problems degrade to a `stub`
   `AgentResult`; a failure *inside* an agent propagates so the executor's
   retry/heal engages.
3. **Resolve per-principal once.** Only `AgentDispatcher.build_context` does it.
4. **Lazy imports.** The SDK imports no LLM SDK, no SCM, no `specializations`
   package init at module top-level — a slim Job pod imports it cleanly.
