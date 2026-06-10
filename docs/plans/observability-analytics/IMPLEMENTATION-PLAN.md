# DevAI — Observability, Analytics & Hardening Plan

Status: in progress (foundation landing in this change-set)
Owner: platform
Scope: a telemetry adapter family + OpenTelemetry wiring, an analytics API + dashboard
page, an OTel collector for the cluster, and the small set of genuinely-actionable
security fixes surfaced by a full-repo audit.

This document is the single source of truth for the work. It records what the codebase
actually is today (not what the docs promise), where the gaps are, what we are changing,
and what we are deliberately *not* changing (and why).

---

## 1. What DevAI actually is today

DevAI is a mature, blueprint-driven ALM + SRE platform. The active core is the
**Fiber-style pipeline runtime** (`src/devai/pipeline/`, `src/devai/blueprint/`), not the
legacy LangGraph orchestrator (which is bridged in via `pipeline/stages/alm.py`).

Entry points:

| App | Module | Purpose |
|-----|--------|---------|
| ALM API | `devai.webhook.app:create_app` | pipeline runtime, webhooks, chat, registry, settings — `/api/*` |
| SRE API | `devai.sre.server:create_sre_app` | incidents, metrics, costs, scans — `/api/*` (port 8090) |
| MCP Hub | `devai.mcphub.app:create_hub_app` | MCP multiplexer (port 8095) |
| CLI | `devai.cli.commands:app` | pipeline ops |
| Worker | `devai.orchestration.worker:main` | Temporal activity worker (opt-in) |

Adapter families present and wired (each: ABC + factory + noop + lazy-imported backends):
`llm, memory, event_bus, secrets, web_search, object_store, identity, observability,
workflow, registry, messaging`. The pattern is strict — no vendor SDK is imported into
business logic; the factory never raises and degrades to `noop`.

Telemetry data **already persisted** (Postgres, schema owned by tesserix-k8s
db-schema-bootstrap):

- `pipeline_runs` — repo, trigger, stage, created/completed timestamps
- `agent_executions` — per-agent `provider, model, tokens_input, tokens_output,
  llm_cost_usd, duration_ms, status, started_at`
- `a2a_messages`, `audit_log`, `approval_gates`, `security_findings`
- SRE: `sre_incidents, sre_metrics, sre_cost_reports, sre_scan_runs, sre_remediations`
- Views: `v_agent_stats, v_recent_activity, v_security_posture, v_sre_cluster_health,
  v_sre_app_reliability`

So the raw material for analytics exists; what's missing is **aggregation endpoints** and a
**page** to render them, plus a real telemetry backend (traces/metrics) wired to the
`DEVAI_OTEL_ENDPOINT` config that today points at nothing.

---

## 2. Gaps found (full-repo sweep)

### Observability
- `DEVAI_OTEL_ENDPOINT` and `DEVAI_METRICS_ENABLED` exist in `config.py` but are wired to
  nothing. `opentelemetry-api` / `opentelemetry-sdk` are in `pyproject.toml` but no
  provider, no exporter, no instrumentation. **→ build `adapters/telemetry/` + wire it.**
- Pipeline stages and LLM calls emit no spans/metrics. LangSmith tracing exists but is a
  separate, optional, LLM-only path. **→ emit OTel spans/metrics from the runtime.**
- Cluster (tesserix-k8s) has Prometheus + Grafana **charted but currently disabled** in
  `argocd/prod/infrastructure/kustomization.yaml` (Kiali is the only enabled monitoring
  app; the note says "using GCP native monitoring or re-enable when needed"). There is **no
  OTel collector, no Tempo/Jaeger, no Loki**. **→ add an `otel-collector` thirdparty chart
  that ingests OTLP and exposes a Prometheus scrape endpoint, so it works standalone today
  (traces/logs → debug) and feeds Prometheus the moment that chart is re-enabled — its
  default kubernetes-pods job scrapes the collector via the pod's `prometheus.io/scrape`
  annotation, no scrape-config edit needed.**

### Analytics
- An `/analytics` route was stubbed and removed (DASH-9) as dead "coming soon". Neither
  dashboard has a charting library. **→ build a real `/analytics` page with `recharts`.**
- No aggregate endpoints (success rate, runs/day, avg duration, token/cost rollups).
  **→ add `src/devai/analytics/` router + `database.py` aggregate queries.**

### Security (audit results, triaged)
See §4 — most raw findings were either already mitigated or intentional. The genuinely
actionable, safe fixes are applied here.

### Known larger gaps (out of scope for this change-set, recorded for later)
- SDK/ADK unification (one Agent contract) — `docs/agentic/IMPLEMENTATION-PLAN-SDK-ADK.md`.
- Temporal as the durable backbone for the cursor-parity plans — design only.
- No integration tests; image-build CI jobs (sre/dashboard/auth-bff) skip tests.
- No CI schema-validation guard for blueprints/specs/registry-seeds.

---

## 3. What we are building (this change-set)

1. **`adapters/telemetry/`** — `TelemetryAdapter` ABC + `factory` (reads
   `DEVAI_TELEMETRY_PROVIDER`) + `noop` + `otel` (lazy OTLP/HTTP exporter). Factory never
   raises; missing exporter SDK → noop.
2. **Wiring** — `webhook.app` and `sre.server` build the adapter on startup, instrument
   the ASGI app (request spans + duration/count metrics), and expose it on `app.state`.
   The pipeline records a span + metrics per stage; the LLM provider records token/cost
   metrics. All guarded by `metrics_enabled` and tolerant of a `None`/noop adapter.
3. **Analytics API** — `src/devai/analytics/` router mounted at `/api/analytics/*`:
   - `GET /api/analytics/summary` — run totals, success rate, avg duration, active runs.
   - `GET /api/analytics/runs/timeseries?days=N` — runs/day by status.
   - `GET /api/analytics/agents` — per-agent count, avg duration, tokens, cost (from
     `v_agent_stats` + `agent_executions`).
   - `GET /api/analytics/llm/cost?days=N` — token + USD cost by provider/model over time.
   - `GET /api/analytics/telemetry` — telemetry/collector health: provider, endpoint,
     exporting?, plus live Prometheus reachability via the observability adapter.
4. **Analytics page** — `dashboard/src/app/analytics/page.tsx`, restored to the nav, using
   the design-token system (`panel`, `pill`, `dot`) and `recharts`. KPI cards + charts for
   runs, success rate, agent/LLM cost & tokens, an SRE summary strip, and a telemetry/OTel
   health panel.
5. **OTel collector** — `tesserix-k8s/charts/thirdparty/otel-collector/` + ArgoCD
   Application `argocd/prod/infrastructure/otel-collector.yaml` + kustomization entry,
   following the redis-global pattern. OTLP receivers (gRPC/HTTP) → Prometheus exporter
   (scraped by existing Prometheus) + debug. `DEVAI_OTEL_ENDPOINT` wired into devai-api /
   devai-sre values to point at the collector.

---

## 4. Security findings — triage & disposition

| # | Finding | Severity (raw) | Disposition |
|---|---------|----------------|-------------|
| 1 | Live creds in `k8s/secrets.yaml` | CRITICAL | **Not a leak** — file is gitignored (`**secrets.yaml`) and not tracked / not in history. Local-only dev file. No repo change. Recommend rotating the on-disk creds since the repo is public and the file could be shared by accident. |
| 2 | `sslmode=disable` (prod `_helpers.tpl`) | HIGH | **Mitigated** — in-mesh CNPG with Istio STRICT mTLS encrypts transport. Left as-is; noted. |
| 3 | Missing `securityContext` (local `k8s/chart`) | HIGH | **Fixed** — brought the local kind chart to parity with the already-hardened prod chart (`runAsNonRoot`, `drop: [ALL]`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`). Prod chart already had it. |
| 4 | `trust_forwarded_without_secret=True` default | HIGH | **Intentional** — documented migration path; flipping the code default fails-closed and breaks prod auth today (auth-bff doesn't yet stamp `X-Auth-Bff-Secret`). Hardened via prod values, not the default. |
| 5 | `shell_exec` uses `create_subprocess_shell` | HIGH | **By design** — sandbox-only "terminal" capability gated on `ctx.workdir` (runner-pod worktree); refuses in the orchestrator process. Left as-is. |
| 6 | kubectl `event_type` field-selector | MEDIUM | **Fixed (defense-in-depth)** — `_kubectl` already uses `create_subprocess_exec` (no shell), but added an allowlist on `event_type ∈ {Warning, Normal}`. |
| 7 | SRE CORS `allow_methods/headers=["*"]` + credentials | MEDIUM | **Fixed** — origins were already restricted; tightened methods/headers to an explicit list. |
| 8 | Admin emails in `k8s/secrets.yaml` | LOW | Same as #1 — local-only, gitignored. |
| 9 | Missing RBAC on local chart SA | LOW | Local kind only; prod RBAC is in tesserix-k8s. Noted. |

Net: 3 code/chart fixes applied (#3, #6, #7); the rest are mitigated, intentional, or
not-a-leak with a rotation recommendation.

---

## 5. Verification

- Python: `ruff check src/`, `python -m compileall`, `pytest tests/unit/test_telemetry_adapters.py`
  (new contract test) + the existing suite.
- Dashboard: `pnpm build` / `tsc --noEmit` in `dashboard/`.
- Helm: `helm template` the new `otel-collector` chart.
- No container builds / pushes / deploys (per workspace rule 0) — the user runs those.

---

## 6. Rollout (user-run, after merge)

1. Deploy the collector: ArgoCD syncs `otel-collector` (already registered in the infra
   kustomization), namespace `observability`. It works immediately — OTLP in, traces/logs
   to the debug exporter (collector logs), metrics held at `:8889/metrics`.
2. The env wiring is already in `devai-api` / `devai-sre` values
   (`DEVAI_TELEMETRY_PROVIDER=otel`, `DEVAI_OTEL_ENDPOINT=…observability…:4318`). Once the
   new images (with the OTLP exporter dep) are built + rolled, the adapter starts exporting.
3. The analytics page's telemetry panel turns green once the adapter reports `exporting`;
   the Prometheus chip stays amber until the Prometheus chart is re-enabled.
4. To get metrics into Prometheus/Grafana, re-enable `prometheus.yaml` (+ `grafana.yaml`)
   in `argocd/prod/infrastructure/kustomization.yaml`; the default kubernetes-pods job
   scrapes the collector automatically via its annotation. A Tempo/Jaeger traces backend
   is one exporter added to the collector config — no app change.
