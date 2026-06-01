# 04 — Plan Mode

**Cursor parity:** Plan Mode (Shift+Tab). **Priority: P1.**

> **Durability ([00 Temporal](00-temporal-orchestration.md)):** the approval gate is
> a Workflow that **waits on a Signal** — it can hold for days with no pod keeping
> state; plan edits arrive as Updates. No polling, no lost approvals.

## What Cursor does

Before writing code, the agent asks clarifying questions and produces a
**reviewable, editable Markdown plan**. The human trims/edits it; the plan is
saved (`.cursor/plans/`) and then drives execution. Separates *thinking* from
*doing*.

## How it works (concepts to steal)

1. **Plan as a first‑class artifact**, not hidden chain‑of‑thought — a structured
   doc (goal, steps, files, risks, acceptance) the human can edit.
2. **Clarify‑before‑code.** Agent surfaces ambiguities up front.
3. **Plan → execution binding.** Approved steps become the work list.
4. **Editable & versioned** in the repo.

## DevAI mapping (framework)

DevAI already has the right substrate: **blueprints/stages (Fiber)** + **approval
gates** (`approval_gates` table, `approval-banner` dashboard component).

- A `plan` stage runs first: the planner agent emits a **Plan artifact**
  (structured JSON + rendered Markdown) → persisted + stored at `.devai/plans/`
  in the repo via `scm/`.
- The plan hits an **approval gate**: dashboard shows it, human edits/approves
  (we already have approve/reject UX).
- Approved plan becomes the **stage list** the orchestrator executes — each plan
  step maps to a blueprint stage / agent task.
- Re‑planning loop if execution diverges (A2A `escalation` → back to planner).

## Implementation plan

- **Phase 1 — Plan artifact + schema** (`plan_artifacts` table in tesserix‑k8s).
- **Phase 2 — planner stage** in the ALM blueprint; clarifying‑questions pass.
- **Phase 3 — approval gate + dashboard edit** (reuse approval banner; add inline
  Markdown edit).
- **Phase 4 — plan→stages binding**; persist plan to repo `.devai/plans/`.

## Files & modules

```
src/devai/blueprint/plan.py            # Plan artifact model + renderer
src/devai/agents/product_director.py   # exists — becomes the planner
src/devai/pipeline/stages/plan_stage.py
dashboard/src/components/plan-panel.tsx
tests/unit/test_plan_mode.py
```

## Config (`DEVAI_*`)

```
DEVAI_PLAN_MODE_ENABLED=true
DEVAI_PLAN_REQUIRE_APPROVAL=true
DEVAI_PLAN_PERSIST_TO_REPO=true        # writes .devai/plans/<run>.md
```

## Acceptance criteria

- A run pauses at a human‑readable plan before any code change.
- Editing the plan (removing a step) changes what executes.
- Approved plan is committed to `.devai/plans/` in the target repo.

## Sources

- [Introducing Plan Mode · Cursor](https://cursor.com/blog/plan-mode)
