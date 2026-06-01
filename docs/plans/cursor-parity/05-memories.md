# 05 — Memories

**Cursor parity:** Memories (replaced Notepads). **Priority: P1.**

## What Cursor does

A persistent knowledge base the agent maintains **automatically** across
sessions — learns your conventions, preferred libraries, project preferences —
and injects the relevant bits into future prompts.

## How it works (concepts to steal)

1. **Auto‑capture.** The agent writes durable facts itself (not just user notes).
2. **Scoped + typed.** Per‑repo / per‑org; facts vs preferences vs how‑tos.
3. **Relevance recall.** Only inject memories relevant to the current task
   (semantic match), not the whole store.
4. **Editable & forgettable.** Wrong memory → delete.

## DevAI mapping (framework)

**Largely already built.** `adapters/memory` exists with episodic / semantic /
procedural types, Redis hot cache + pgvector semantic search, and the pipeline
already does "agent memory injection (cross‑run learning)". This plan = close the
*auto‑maintain* loop and the *relevance recall* surface.

- **Auto‑capture stage:** after each run, a `learn` step distills durable facts
  (repo conventions, recurring fixes, stack quirks) → `remember()` with type +
  repo scope. (SRE pipeline already has a `learn` node — mirror it for ALM.)
- **Relevance recall:** before each agent runs, `recall(task, repo, k)` via the
  semantic adapter; inject top‑k into the system prompt.
- **Governance:** dashboard view to list/edit/forget memories; TTL on episodic.
- Backends stay swappable (`mem0|zep|pgvector|redis|noop`) per the adapter rule.

## Implementation plan

- **Phase 1 — recall surface.** `recall(task, repo, k)` + prompt injection helper
  used by every agent (one call site in `StageDeps`).
- **Phase 2 — auto‑capture.** ALM `learn` stage; extraction prompt → typed
  `remember()`.
- **Phase 3 — governance UI** (list/edit/forget) + TTLs.
- **Phase 4 — dedupe/decay** so the store stays sharp (loop‑until‑dry style).

## Files & modules

```
src/devai/adapters/memory/*            # exists
src/devai/graph/stages/learn_stage.py  # ALM learn (mirror SRE learn node)
src/devai/memory/recall.py             # recall+inject helper
dashboard/src/components/memory-panel.tsx
tests/unit/test_memory_adapters.py     # exists — extend
```

## Config (`DEVAI_*`)

```
DEVAI_MEMORY_PROVIDER=pgvector         # mem0|zep|pgvector|redis|noop
DEVAI_MEMORY_AUTOCAPTURE=true
DEVAI_MEMORY_RECALL_K=6
DEVAI_MEMORY_EPISODIC_TTL_DAYS=30
```

## Acceptance criteria

- After a run, new repo‑scoped memories appear automatically.
- A later run on the same repo recalls and uses a prior convention (observable in
  the prompt/trace).
- Forgetting a memory stops it being recalled.
- Provider swap leaves behaviour identical (contract tests green).

## Sources

- [Mastering Cursor: Rules, Skills, Memories · Medium](https://rzaeeff.medium.com/mastering-cursor-rules-agent-skills-modes-models-and-best-practices-81908ec4f4a4)
- DevAI: `CLAUDE.md` → Agent Memory System
