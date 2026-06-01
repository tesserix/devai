# 10 — Automations + `/loop`

**Cursor parity:** Cursor Automations + `/loop` skill (v3.5). **Priority: P2.**

> **Durability ([00 Temporal](00-temporal-orchestration.md)):** schedules are
> **Temporal Schedules** (replacing the k8s CronJob); wake conditions are Signals;
> long/looping agents use `continue_as_new` so history stays bounded. A missed
> tick is visible and recoverable.

## What Cursor does

- **Automations**: scheduled agents in the Agents Window, **multi‑repo** and even
  **no‑repo** (pure tool‑monitoring), with marketplace templates (Slack digest,
  analytics, FAQ, finance, customer‑health).
- **`/loop`**: repeat a prompt on a local schedule until an outcome is reached;
  **long‑running agents** with custom **wake conditions**.

## How it works (concepts to steal)

1. **Trigger ≠ runtime.** A schedule/condition enqueues the same task unit.
2. **Wake conditions** — run again when X changes (not just fixed cron).
3. **Multi‑repo / no‑repo** scope — an agent can span repos or watch a signal.
4. **Loop‑until‑outcome** with a budget/stop condition.

## DevAI mapping (framework)

DevAI already runs the **SRE pipeline on a 5‑min cron** — generalise that into a
first‑class scheduler over the background‑task dispatcher (plan 02).

- **`scheduler/`** subsystem: cron + condition triggers → `dispatch(BackgroundTask)`.
- **Wake conditions**: webhook/event‑bus predicates (`event_bus` adapter exists:
  nats/redis) — e.g. "new high‑sev SRE incident", "dependency CVE", "PR stale 3d".
- **Multi‑repo**: a task targets a repo set; fan‑out via plan 09.
- **No‑repo automations**: tasks bound to a tool/signal, not a repo (e.g. "daily
  SRE cost digest to Slack" — we already have `sre_cost_reports`).
- **Loop‑until**: stop condition + token/time budget on the task.
- **Templates**: a catalogue (registry) of prebuilt automations.

## Implementation plan

- **Phase 1 — scheduler** (cron) over `dispatch()`; persist `automations` table.
- **Phase 2 — condition/wake triggers** on the event bus.
- **Phase 3 — multi‑repo / no‑repo task scoping** + loop‑until + budget.
- **Phase 4 — template catalogue** + dashboard CRUD.

## Files & modules

```
src/devai/scheduler/{cron,conditions,automations}.py
src/devai/adapters/event_bus/*         # exists (nats/redis) — wake conditions
src/devai/runtime/background.py        # plan 02 dispatch
dashboard/src/components/automations-panel.tsx
tests/unit/test_scheduler.py
```

## Config (`DEVAI_*`)

```
DEVAI_AUTOMATIONS_ENABLED=true
DEVAI_AUTOMATIONS_MAX_CONCURRENCY=4
DEVAI_AUTOMATION_DEFAULT_BUDGET_TOKENS=500000
```

## Acceptance criteria

- A cron automation fires a background task on schedule and records the run.
- A condition automation fires on an event‑bus signal (e.g. new incident).
- A loop‑until automation stops at its outcome or budget, whichever first.
- A no‑repo automation (cost digest) runs without a target repo.

## Sources

- [Cursor Changelog v3.5 — Automations & `/loop`](https://cursor.com/changelog)
