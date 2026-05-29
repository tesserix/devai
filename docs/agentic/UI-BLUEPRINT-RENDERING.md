# Blueprints as the UI Source of Truth — Assessment & Plan

> **Goal:** the dashboards render workflows/pipelines from the blueprint
> definitions themselves, so a new blueprint — or a UI-created custom
> agent/workflow — shows up as a nice flow with **no UI code change**.
>
> Companion to `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md`. This is the
> *rendering* half of the zero-code-custom-agent promise.
>
> _Findings below were verified against the code by a multi-agent review._

---

## 1. How it's wired today

```
SCM webhook ─┐
CLI / REST   ├─▶ PipelineService ──▶ Pipeline ──▶ BlueprintExecutor
Chat / cron ─┘     (service.py)      (pipeline.py)   (executor.py)
                        │                                │
                        │  loads blueprints/*.yaml       │ topo-sorts StageSpec by depends_on,
                        │  via blueprint/loader.py        │ runs levels (parallel via asyncio.gather),
                        │  → Blueprint{stages:[StageSpec]} │ emits StageEvent per (stage, phase)
                        ▼                                ▼
        ┌──────── event fanout (service._on_event) ───────────┐
        │  SSE  /api/pipeline/events/stream                    │
        │  NATS devai.pipeline.stage.{stage}.{phase}           │
        │  Redis devai:run:{id}:events / :a2a_messages         │
        └──────────────────────────────────────────────────────┘
                        ▼
        ALM dashboard (Next.js)              SRE dashboard (Next.js)
        pipeline-flow.tsx renders            page.tsx renders NAV_ITEMS +
        PIPELINE_STAGES (hardcoded)          SRE_AGENTS (hardcoded)
```

- **Backend is genuinely blueprint-driven** — stages, DAG, conditions,
  timeouts, parallelism, failure policy all from YAML (`blueprint/loader.py:65-103`,
  `blueprint/executor.py`).
- **Transport is data-driven** — `StageEvent` carries `stage`, `phase`,
  `duration_ms`; NATS topics are parameterized by stage name
  (`pipeline/service.py:466-529`).
- **API exposes runtime state** (`/api/pipeline/runs`, `/runs/{id}`,
  `/events/stream`) and a blueprint *summary* — but not the blueprint graph.
- **UI is decoupled from blueprints entirely** and renders static node arrays.

## 2. Verdict — HARDCODED (both dashboards)

**ALM dashboard.** `dashboard/src/lib/constants.ts:1-17` defines
`PIPELINE_STAGES` as a literal 14-element `as const` array with fixed labels +
Tailwind colors; `STAGE_TO_AGENT` is a static map. `pipeline-flow.tsx` imports
and iterates them; `page.tsx:204-207` passes only `selectedRun.stage` (a
string). `dashboard/src/lib/api.ts` types `PipelineRun.stage: string` — no
blueprint field, no stage list. **`blueprints/alm-pipeline.yaml` defines 18
stages, so the UI's 14-node view is already out of sync** with the backend.

**SRE dashboard.** Backend is blueprint-driven (`sre/server.py:388-432` runs
`blueprint="sre-monitor"`), but the frontend hardcodes `NAV_ITEMS`
(`sre-dashboard/src/app/page.tsx:20`) and `SRE_AGENTS`
(`sre-dashboard/src/lib/constants.ts:1-40`). Agent metadata lives in **three**
places (constants.ts, sre-monitor.yaml, sre.py). `scan_history.tsx` doesn't
render the `agent_timings` it receives as a flow.

**Why the UI *can't* be data-driven today:** `PipelineService.list_blueprints()`
(`pipeline/service.py:377-391`) returns only `{name, description, stage_count,
metadata}` and **drops the `stages` array**. `/api/pipeline/blueprints`
(`routes.py:40-42`) returns that summary; `/api/pipeline/stages` returns bare
factory keys. There is **no `GET /api/pipeline/blueprints/{name}`**. Hardcoding
is currently the only option.

## 3. Gaps blocking "blueprints as the UI source of truth"

1. **No blueprint-definition endpoint** returning `StageSpec[]` with
   `depends_on`/`condition`/`type`/`config`. `list_blueprints()` truncates to a count.
2. **`StageSpec` has no `to_dict()` and no render metadata** (`loader.py:65-93`):
   it has `name`, `stage`, `type`, `depends_on`, `condition`, `timeout_seconds`,
   `on_failure`, `config`, `parallel` — but no title, description, color, lane, icon.
   The `type` comment even says "informational for the dashboard," yet it never reaches it.
3. **Conditions aren't structured for rendering** — `condition` is a free-text
   expression (`blueprint/conditions.py`); a UI can't enumerate branch edges from it.
4. **Parallel stages have no group identity** — `parallel: true` exists but no
   group name; the executor's topo-sort levels (`executor.py:256`) are never serialized.
5. **UI hardcodes the node array + stage→agent map** (`constants.ts`); multi-blueprint
   rendering is impossible.
6. **`/api/pipeline/stages` returns keys only** — no per-stage description/type.
7. **`StageEvent` lacks stage context** — only `stage`+`phase`; no `type`, agent, or gate flag.
8. **No hot-reload / POST for blueprints** — blueprints load once at startup
   (specializations can already `/specializations/reload`).

## 4. Recommended design (lean, not over-engineered)

**One render-ready graph endpoint + a small block of optional YAML render
metadata + one generic renderer + overlay the existing event stream.** No new
transport, no new DB tables.

### (a) One endpoint that serves a render-ready graph
`GET /api/pipeline/blueprints/{name}` → `PipelineService.get_blueprint_graph(name)`:

```jsonc
{
  "name": "alm-pipeline",
  "title": "Application Lifecycle",
  "description": "...",
  "lanes": ["plan", "build", "review", "deploy"],
  "nodes": [
    { "id": "implement_code", "title": "Code", "type": "agentic",
      "lane": "build", "color": "amber", "agent": "senior_developer",
      "gate": false, "timeout_seconds": 900, "parallel_group": null }
  ],
  "edges": [
    { "from": "create_plan",  "to": "implement_code", "kind": "sequence" },
    { "from": "review_code",  "to": "implement_code", "kind": "conditional",
      "label": "review_decision == changes_requested" }
  ]
}
```

`nodes` come straight from `StageSpec`; `edges` are derived **server-side** from
`depends_on` (sequence) + `condition` (conditional, raw expression as `label`);
`parallel_group` from the `_topo_sort` levels (`executor.py:256`). The server
does the graph math once; the UI just draws.

### (b) Optional render metadata in the YAML schema
Minimal + optional (fallbacks so existing blueprints still render). In
`blueprint/loader.py`:
- Extend `_ALLOWED_STAGE_KEYS` + `StageSpec` with `title` (default = humanized
  `name`), `lane`, `color`, `agent` (specialization key), `gate` (human approval bool).
- Extend `Blueprint.metadata` (already free-form) with `title`, `lanes` (order).
- Add `StageSpec.to_dict()` + `Blueprint.to_graph_dict()` so serialization lives
  next to the model (mirrors `pipeline/types.py`). Where a node maps to a
  specialization, join `role_color`/`display_name` from `specializations/base.py`
  instead of duplicating — kills the SRE "three sources of truth" problem.

### (c) Generic data-driven renderer
- Add `getBlueprintGraph(name)` to `dashboard/src/lib/api.ts` and
  `sre-dashboard/src/lib/api.ts`, typed `{nodes, edges, lanes}`.
- Rewrite `pipeline-flow.tsx` to render nodes grouped by `lane`, connectors from
  `edges` (solid = sequence, dashed/labeled = conditional), color/title/gate-icon
  from node fields. One component renders alm-pipeline, pr-review, sre-monitor,
  security-scan, app-scaffold — and any new/UI-created blueprint — with no code change.
- Delete `PIPELINE_STAGES`/`STAGE_TO_AGENT`/`SRE_AGENTS` once live.

### (d) Live status overlay via existing events
No new plumbing. The renderer holds the static graph and overlays runtime state
by keying `node.id == StageEvent.stage`:
- Subscribe to `/api/pipeline/events/stream` (already SSE, `routes.py:142-174`);
  color each node by latest `phase` (started→active, completed→done, failed→red,
  skipped→dim).
- Use event `duration_ms` vs node `timeout_seconds` for progress / approaching-timeout.
- Add `type` + `gate` to the `StageEvent` payload in `service._on_event()`
  (`service.py:466`) so the UI can label gates/types live without cross-referencing.

### Tie-in to SDK/ADK + zero-code custom agents
A custom agent (specialization) referenced from a blueprint stage with
`title`/`lane`/`color`/`agent` becomes a fully rendered node the moment
`get_blueprint_graph` serializes it — **no UI change**. To close the loop for
*runtime-created* blueprints, add `POST /api/pipeline/blueprints` (reuse
`load_blueprint_from_string`) + a reload path mirroring `/specializations/reload`.

## 5. Phased steps (small, shippable)

**Phase 1 — Expose the graph (backend only, no UI risk). ✅ SHIPPED.**
`StageSpec.to_dict()` + render fields (`title`/`lane`/`color`/`agent`/`gate`) in
`blueprint/loader.py`; `build_blueprint_graph()` in `blueprint/graph.py` (edges
from `depends_on`/`condition`, `parallel_group` from `executor._topo_sort`
levels); `PipelineService.get_blueprint_graph()`; `GET /api/pipeline/blueprints/{name}`
in `pipeline/routes.py` (404 on miss). Humanized-name/config-agent fallbacks so all
5 shipped blueprints serialize unchanged. Tests: `tests/unit/test_blueprint_graph.py`
(10 cases, all green; 40 incl. existing pipeline tests).

**Phase 2 — Optional render metadata in YAML. ✅ SHIPPED.**
`StageSpec`/`_ALLOWED_STAGE_KEYS` extended with `title`/`lane`/`color`/`agent`/`gate`;
blueprint-level `title`/`lanes` read from `metadata` (no schema break). Backfilled
`alm-pipeline.yaml` (lanes plan/build/review/deploy, gates on staff-review +
deploy-release) and `sre-monitor.yaml` (lanes discovery/monitor/respond, 5-way
parallel monitor group). Execution fields left byte-identical; all 40 tests green.
(Deferred polish: join color/title from the matching Specialization — fallbacks cover it.)

**Phase 3 — Generic ALM renderer. ✅ SHIPPED.** `api.getBlueprintGraph()` +
`BlueprintGraph*` types in `dashboard/src/lib/api.ts`; `pipeline-flow.tsx` rewritten
as a **custom SVG DAG renderer** (lanes as columns, nodes as rects, `depends_on`
edges as bezier paths — dashed + labeled for conditional, parallel-group dashed
boxes, gate badges, status overlay from `currentStage`). No new dependency. Removed
the hardcoded `PIPELINE_STAGES`/`STAGE_TO_AGENT` from `constants.ts` (kept
`AGENT_INFO`/`AGENT_HIERARCHY`/`EXECUTION_PHASES` — used elsewhere). `page.tsx`
unchanged (same prop interface; optional `blueprint` prop added). `tsc --noEmit`
clean. Now renders the real 20-node alm-pipeline with 4 lanes.

**Phase 4 — Live overlay + SRE renderer.** _Backend half ✅:_ `StageEvent` now
carries `type` + `gate` (`pipeline/types.py`), stamped from the `StageSpec` at the
STARTED/COMPLETED/FAILED emit sites (`blueprint/executor.py`) — SSE consumers can
label nodes without re-reading the blueprint. _Remaining:_ subscribe `pipeline-flow`
to `/api/pipeline/events/stream` and overlay live phase/timing onto nodes; point the
SRE dashboard's flow at `getBlueprintGraph("sre-monitor")`, replacing `SRE_AGENTS`.

**Phase 5 — Runtime blueprint registration.** `POST /api/pipeline/blueprints`
(via `load_blueprint_from_string`) + reload, mirroring `/specializations/reload`.
A UI-authored blueprint/agent appears as a rendered flow with no UI deploy —
completing the SDK/ADK zero-code-custom-agent goal.

---

**Key files:** `dashboard/src/lib/constants.ts:1-17`,
`dashboard/src/components/pipeline-flow.tsx`, `dashboard/src/app/page.tsx:204-207`,
`dashboard/src/lib/api.ts`, `sre-dashboard/src/app/page.tsx:20`,
`sre-dashboard/src/lib/constants.ts:1-40`, `sre-dashboard/src/components/scan_history.tsx`,
`src/devai/pipeline/service.py:377-391,466-529`, `src/devai/pipeline/routes.py:40-47,142-174`,
`src/devai/blueprint/loader.py:65-103`, `src/devai/blueprint/executor.py:256`,
`src/devai/blueprint/conditions.py`, `src/devai/pipeline/types.py`,
`src/devai/pipeline/stages/sre.py`, `src/devai/sre/server.py:388-432`,
`src/devai/specializations/base.py`, `blueprints/alm-pipeline.yaml`,
`blueprints/sre-monitor.yaml`, `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md`.
