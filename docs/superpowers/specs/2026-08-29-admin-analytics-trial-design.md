# Admin analytics tab + trial allowance UX

Date: 2026-08-29
Status: approved for planning

## Problem

Two related gaps:

1. Platform owners have no view of *who* uses DevAI. `/analytics` shows runs,
   stages, cost and telemetry, but nothing about human users — no active-user
   count, no sign-in figure, no per-user activity that only owners can see.
2. New users have no guided way to try the platform. A trial token allowance
   exists in the backend but is dormant and invisible: nothing in the dashboard
   reads it, so a user is never told they have free tokens, never sees them
   deplete, and never gets a prompt to add their own key.

## What already exists

This shapes the design more than anything else — most of the backend is built.

| Capability | Location | State |
|---|---|---|
| `admin` role from an email allowlist | `config.py:297` (`admin_emails`), `identity.py:422` (`_apply_admin_role`) | Working |
| Stricter `platform-admin` role | `settings/routes.py`, `sandbox/routes.py:96` | Working |
| Per-user LLM call/token/cost ledger | `analytics/usage_ledger.py` (Redis) | Working |
| Admin-only global + by-user usage view | `analytics/routes.py:155` (`_usage_scope`) | Working |
| Trial meter, metered adapter, exhaustion message | `settings/trial.py` | Working, dormant |
| Trial status endpoint | `settings/routes.py:489` (`GET /api/settings/trial`) | Working, no UI consumer |
| Trial wiring into LLM resolution | `pipeline/interfaces.py:136-146`, `:238-248` | Working, gated off |
| Analytics page with tabs and chart primitives | `dashboard/src/app/analytics/page.tsx`, `components/charts.tsx` | Working |
| `audit_log` append-only user/agent event table | `db/migrations/0001_initial_schema.up.sql:158` | Working |
| OpenPanel (ClickHouse web analytics) | `tesserix-k8s/charts/thirdparty/openpanel`, `analytics.tesserix.app` | Deployed, DevAI not onboarded |

Two constraints follow from the existing system and are load-bearing below:

- **DevAI never observes a login in production.** `auth-bff` terminates OAuth
  externally and forwards `X-Forwarded-*` identity headers (`identity.py`). The
  `/auth/login` route in `dashboard/local_auth_routes.py` is the local-dev path
  only. The backend sees authenticated *requests*, not sign-in *moments*.
- **No new SQL in this repo.** CLAUDE.md §5 requires schemas to live in
  `tesserix-k8s`. Any design needing a new table would be blocked on a
  cross-repo change before it could ship.

## Decisions

| # | Decision | Rejected alternative |
|---|---|---|
| 1 | Backend is the system of record for user activity; OpenPanel enriches with page-level browsing | OpenPanel as sole source (ad-blockers undercount an auth fact the backend knows exactly) |
| 2 | Admin surface is a tab on `/analytics`, enforced server-side | Separate `/admin` route with `middleware.ts` path gating |
| 3 | Demo ideas at onboarding; meter throughout; upgrade prompt at exhaustion | Exhaustion-only (shows suggestions when the user can no longer act on them) |
| 4 | Ship the meter observable but unenforced; flip strict mode later from evidence | Enable strict mode now (budget would be a guess, and exhaustion is permanent) |
| 5 | Reuse `audit_log` for user-activity events | New table (forbidden here; would require `tesserix-k8s` first) |

Decision 2 was chosen by the user over the recommended separate route. The
recommendation rested on `middleware.ts` being able to gate by path; a tab
inside `/analytics` cannot be gated there. The mitigation is that authorization
lives entirely in the backend and is applied at the router, not per-handler, so
enforcement does not depend on the edge or on any client-side check.

## Metric definitions

Naming here is deliberate — each metric is labelled by what the data actually
supports, so an approximate figure never reads as an exact one.

- **Active users (daily)** — distinct authenticated principals that made at
  least one request that day. Exact. Derived from `audit_log`.
- **Sign-ins** — explicit login events. Exact in local dev (`/auth/login`);
  in production reported client-side by OpenPanel and labelled as such.
- **LLM calls / tokens / cost per user** — from `UsageLedger`. Exact.
- **Page hits, sessions, referrers** — from OpenPanel. Approximate.

The user's phrasing "how many users logged in" maps to **active users** on the
page. Sign-ins are shown separately rather than conflated with it.

## Architecture

### Backend — `src/devai/admin/`

New package, following the shape of existing route packages:

```
src/devai/admin/
  __init__.py
  routes.py     # /api/admin/* router
  service.py    # rollup queries over audit_log + UsageLedger
  openpanel.py  # OpenPanel API client (lazy import, degrades to disabled)
```

- `require_admin(request)` — FastAPI dependency resolving the principal via
  `extract_principal` and requiring `admin` or `platform-admin`, else HTTP 403.
  Applied to the **router**, so a later endpoint cannot omit it.
- `GET /api/admin/overview` — active-user timeseries, sign-in totals, and a
  per-user rollup joining `audit_log` activity with `UsageLedger.by_user`.
- `GET /api/admin/openpanel` — server-side proxy to OpenPanel. Returns
  `{"enabled": false}` when unconfigured. The client key stays server-side.

### Activity recording

`ActivityMiddleware` writes one `audit_log` row per user per day with
`action="user_active"`, `actor=<email>`, `actor_type="user"`. A Redis key
(`devai:activity:{date}:{email}`, TTL 48h) makes it one write per user per day
rather than one per request. Wrapped in try/except — recording must never fail
a user request, matching the best-effort posture of `UsageLedger`.

Local sign-ins additionally write `action="login"` from `local_auth_routes.py`.

### Frontend

- New `admin` entry in the `TABS` array of `analytics/page.tsx`, rendered only
  when `GET /api/admin/overview` returns 200. The API decides; the client does
  not test emails or roles itself.
- Reuses `Donut`, `HBarChart`, `LineChart` from `components/charts.tsx` and
  design tokens only, per that file's existing conventions.
- The OpenPanel section loads and degrades independently, preserving the page's
  documented contract that one missing source never blanks the page.

### Trial UX — no backend changes

- `TrialProvider` reads `GET /api/settings/trial`.
- First login: dismissible demo panel with concrete things to try
  (`localStorage`-remembered).
- Persistent remaining-tokens meter in the shell.
- Warning banner at >=80% consumed — the threshold `settings/trial.py:9`
  already anticipates.
- Exhaustion modal linking to Settings to add an own connector.

## Configuration

| Setting | Value | Note |
|---|---|---|
| `DEVAI_ADMIN_EMAILS` | `samyak.rout@gmail.com,mahesh.sangawar@gmail.com` | Grants the `admin` role |
| `DEVAI_LLM_TRIAL_TOKEN_BUDGET` | non-zero | Makes the meter visible |
| `DEVAI_LLM_REQUIRE_USER_CONNECTOR` | `false` (unchanged) | Keeps the trial unenforced |
| `DEVAI_OPENPANEL_API_URL` | unset initially | e.g. `https://analytics.tesserix.app/api` |
| `DEVAI_OPENPANEL_CLIENT_ID` | unset initially | DevAI project's client ID |
| `DEVAI_OPENPANEL_CLIENT_SECRET` | unset initially | Server-side only, never sent to the browser |

Any of the three unset means `/api/admin/openpanel` reports `enabled: false`.

Enforcement is deliberately deferred. `TrialMeter` counters never reset, so an
exhausted user is permanently revoked from the shared keys. Picking a budget
before observing real consumption risks locking out active users. The admin tab
supplies exactly the consumption data needed to choose that number, so strict
mode is flipped afterwards as a one-value follow-up.

## Testing

- Every `/api/admin/*` route returns 403 for a non-admin principal and 200 for
  an admin — asserted per route, so a new unguarded route fails the suite.
- Active-user recording deduplicates within a day and across pods.
- Recording failure (Redis or Postgres down) does not fail the user request.
- `/api/admin/openpanel` returns `enabled: false` and HTTP 200 when unconfigured.
- Trial banner thresholds: hidden below 80%, warning at >=80%, modal at 100%.

## Out of scope

Cross-repo work in `tesserix-k8s`, to be applied by the user, not this branch:

- A `devai` project entry in `charts/thirdparty/openpanel/values-prod.yaml`
  under `config.apps`, with namespace, project name, domain and CORS origin.
- The corresponding client ID in the `openpanel-client-ids` secret.

The DevAI side is written to consume these and reports OpenPanel as disabled
until they exist, so this branch ships and is verifiable without them.

Also out of scope: enabling strict mode, any container build, image push or
deploy, and any change to production Helm values.
