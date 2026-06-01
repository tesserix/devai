# 09 — Parallel Multi‑Agent Orchestration

**Cursor parity:** parallel agents / agent slots (up to 8). **Priority: P1.**

> **Durability ([00 Temporal](00-temporal-orchestration.md)):** fan‑out as **child
> workflows** (one per sub‑task), the parent awaits all, then a merge Activity
> integrates branches. Temporal handles partial failure/retry per child without
> losing the others' work.

## What Cursor does

Run multiple agents at once — backend, frontend, tests, or the same change across
microservices — each in its **own git worktree** so they don't collide; results
merge independently.

## How it works (concepts to steal)

1. **Fan‑out a goal into independent sub‑tasks**, one agent each.
2. **Worktree isolation** per agent (from plan 02).
3. **Bounded concurrency** (slots) + a **merge/synthesis** step.
4. **Isolation prevents interference** — separate files, builds, tests.

## DevAI mapping (framework)

DevAI is *built* for this: **A2A bus** (`graph/a2a.py`) + K8s Job runner + the
"collaborative parallel team, not sequential waterfall" goal already on record.

- **Decomposer** turns a goal/plan (plan 04) into a DAG of independent tasks.
- **Fan‑out** to N background Jobs (plan 02), each its own worktree, capped by a
  slot limit.
- **A2A coordination:** `handoff`, `escalation`, `broadcast` already exist for
  cross‑agent messaging through `ALMState["a2a_messages"]`.
- **Merge stage:** collect branches → integration agent resolves conflicts →
  single PR (or stacked PRs). Adversarial verify (plan 03) on the merged result.
- **Barrier vs pipeline:** independent tasks pipeline; only the merge is a barrier.

## Implementation plan

- **Phase 1 — decomposer** (`graph/decompose.py`) goal→task‑DAG with
  independence detection.
- **Phase 2 — scheduler** with bounded slots over the runner (reuse
  `DEVAI_BACKGROUND_MAX_PARALLEL`).
- **Phase 3 — A2A coordination** for shared decisions (already present — wire it).
- **Phase 4 — merge/synthesis** + conflict resolution + unified PR.

## Files & modules

```
src/devai/graph/{decompose,scheduler,merge}.py
src/devai/graph/a2a.py                  # exists
src/devai/runner/*                      # plan 02 worktrees
dashboard/src/components/a2a-feed.tsx   # exists — show parallel fan-out
tests/unit/test_parallel_orchestration.py
```

## Config (`DEVAI_*`)

```
DEVAI_PARALLEL_ENABLED=true
DEVAI_BACKGROUND_MAX_PARALLEL=8
DEVAI_PARALLEL_MERGE_STRATEGY=integration-agent   # or stacked-prs
```

## Acceptance criteria

- One goal fans out to ≥2 concurrent agents in separate worktrees, no collisions.
- A2A messages between them are persisted and visible in the feed.
- Merge stage produces one coherent PR (or a clean stacked set) that builds.

## Sources

- [Cursor 2.0: Agent‑First Architecture](https://www.digitalapplied.com/blog/cursor-2-0-agent-first-architecture-guide)
- DevAI memory: `project_agent_workflow` (collaborative parallel team)
