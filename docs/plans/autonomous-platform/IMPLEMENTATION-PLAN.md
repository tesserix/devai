# Autonomous Platform — Master Implementation Plan

Goal: DevAI as a fully autonomous development platform — idea → scaffold →
**live preview** → iterate by conversation → review → ship — with every step
observable, governable, and self-healing. Synthesized from two deep audits
(2026-06-11): the full product surface and the develop→preview loop.

## Where the platform stands (audited)

**Solid (ship-grade):** idea→repo→scaffold→onboard; crews/supervisor from
Compose; run engine (durable queue + claim guard + live observability spine);
preview pods (auth-gated, self-healing OOM/port/CORS/install, TTL reaper,
session reuse); registry/analytics/logs/settings; agentic memory (pgvector +
distillation + lifecycle).

**The honest grade: ~60–70% autonomous.** The loop breaks at: previews are a
dead end (no iterate path), deploy is advisory-only, chat can't await runs,
PR automation isn't label-driven, ALM can't be scheduled, single-repo only.

## Phases (impact-ordered)

### Phase A — Preview production hardening  *(the "proper preview" ask)*

| # | Work | Files | Status |
|---|---|---|---|
| A1 | Config portability: `preview_domain` + `preview_gateway` from Settings (currently hardcoded `tesserix.app` / `tesseract-gateway`) | `runtime/job_spec.py`, `runtime/k8s_client.py`, `config.py` | ✅ shipped |
| A2 | Lifecycle: auto-teardown of the run's preview on terminal task state (cleanup stage) + orphaned-pod GC (reverse reconcile: K8s preview Deployments ↔ preview_sessions rows) in the reaper | `pipeline/stages/lifecycle.py`, `preview/service.py` | ✅ shipped |
| A3 | Visibility: `GET /api/preview/{id}/logs` (per-container tail), `POST /api/preview/{id}/reload` (rolling restart → re-clone → fresh code), diagnoses returned to the client instead of generic 422 | `preview/routes.py`, `preview/service.py`, `runtime/k8s_client.py` | ✅ shipped |
| A4 | Dashboard preview panel: status per container, log drawer, Reload button, diagnosis hints | `dashboard/src/components/repo-panel.tsx` / preview pane | ✅ shipped (logs + reload + diagnoses in preview pane) |

### Phase B — The iterate loop  *(scaffold → preview → "make the buttons blue" → updated preview)*

| # | Work | Notes |
|---|---|---|
| B1 | **Continue developing**: button on run detail/preview → Compose pre-filled with {repo, branch, app context} → dispatches a follow-up run on the SAME branch (senior_developer with existing-app control flow) → on completion auto-calls preview reload so the new commit is live | ✅ shipped (run detail → Compose prefill → same-branch dispatch; preview auto-reloads via cleanup-stage reload hook) |
| B2 | **Chat↔run closure**: chat already injects; add `await_run` tool that subscribes to the run-event spine (terminal task envelope) and returns the outcome summary to the conversation — chat can then continue autonomously | ✅ shipped (`check_run_status` + `trigger_pipeline` tools; spine-backed) |
| B3 | **Label-driven PR automation**: webhook handler maps PR labels → blueprints (`devai:review`→pr-review, `devai:automate`→alm-pipeline …), config-driven | ✅ shipped (`DEVAI_LABEL_BLUEPRINTS` map in webhook routes) |

### Phase C — Ship for real

| # | Work | Notes |
|---|---|---|
| C1 | Deploy stage with real mechanics behind the existing human gate: merge the run's PR + tag release (SCM ops we already have) + optional ArgoCD sync trigger; explicit no-op mode for repos without deploy config | release_manager grows from advisory to operational |
| C2 | ALM scheduling: `alm_schedules` (mirror sre_schedules) + scheduler loop in the API + "Schedule" action on Workflows page | nightly runs, recurring audits |

### Phase D — Platform breadth

- D1 Authoring UIs for /skills/new, /tools/new, /prompts/new — wire the existing ArtifactEditor (already aregistry-parity) into the three missing pages.
- D2 Multi-repo coordination: DevAITask.repos[] + for-each-repo stage group (design first; touches task model + executor).
- D3 Polish: one-click onboarding completion, retrigger preserves crew, compose↔run-detail deep links, preview WebSocket auth spike.

## Standing constraints

- Schema changes → tesserix-k8s db-schema-bootstrap (devai-db), never here.
- Prod changes → git → CI → Kargo/ArgoCD only.
- Every phase lands with unit tests, ruff, dashboard build, and a prod
  verification probe, same as the memory + observability work.
