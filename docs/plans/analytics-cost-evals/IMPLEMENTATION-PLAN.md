# Analytics — Cost, Latency, Tokens, Evals (per user)

**Status:** Implementing — 2026-06-12
**Goal:** the /analytics page answers, per user and overall: how much did it COST,
how long did it TAKE, how many TOKENS, and how GOOD were the results (evals).

## Current state (audit)

- `agent_executions` records provider/model/tokens/duration AND has an
  `llm_cost_usd` column — but **cost is never computed** (`complete_agent_execution`
  is always called with `cost_usd=0.0`; no pricing table exists). So the cost UI
  shows $0.
- Analytics API + UI already aggregate by agent and by model (tokens, duration,
  cost, failures), runs timeseries, stages, memory, telemetry health.
- **No per-user attribution** on executions; **no evals** anywhere.

## Plan

### Phase 1 — Real cost (code only, no schema change)
- `src/devai/analytics/pricing.py`: `MODEL_PRICES` (USD per 1M input/output tokens)
  for every model DevAI can use (Claude, Gemini/Vertex, GPT/o-series, Groq, common
  OpenRouter), + `estimate_cost(provider, model, tok_in, tok_out)`. Prefix/contains
  matching so versioned ids (`claude-opus-4-8`, `gemini-2.5-flash`) resolve.
- `complete_agent_execution`: when `cost_usd == 0` and tokens present, compute from
  the table. Existing cost-by-agent/model analytics immediately show real money.
- `GET /api/analytics/pricing` — the rate card for the UI ("how cost is computed").

### Phase 2 — Per-user attribution
- Schema (tesserix-k8s): `agent_executions.triggered_by TEXT` + index.
- `record_agent_execution` stores it; `analytics_*` gain a by-user rollup
  (`GET /api/analytics/users` → cost/tokens/duration/runs per user).
- UI: a "By user" table; cost card stays overall, user table breaks it down.

### Phase 3 — Evals
- Schema (tesserix-k8s): `agent_evals` (run_id, stage, evaluator, score 0..1,
  passed bool, detail jsonb, created_at).
- `src/devai/analytics/evals.py` + `db.record_eval` / `analytics_evals`. Capture
  from the quality gates we already run: review loop (pass/iterations), security
  scan (findings → score), tests (pass rate). A run's eval score = weighted blend.
- `GET /api/analytics/evals` → per-run scores + pass-rate timeseries + by-evaluator.
- UI: an "Evals" section — pass rate, avg score, recent runs with their scores.

### UI (all phases)
analytics page gains: a real **Cost** hero card (USD, with rate-card tooltip),
**By user** table (cost/tokens/time), **Evals** section (pass rate, scores). Each
metric has a one-line plain-language explainer so any user understands it.

## Test + deploy
- Unit: pricing resolution + estimate; eval scoring; per-user rollup shape.
- Schema additions land via tesserix-k8s db-schema-bootstrap (idempotent CronJob).
- Code deploys via CI → ArgoCD as usual.
