# DevAI Live Full‑Stack Preview — Complete Implementation Plan

A Lovable/Bolt‑style **live, hot‑reloading preview that runs in the cluster** — embedded in
the DevAI dashboard (its own Preview tab + side‑by‑side with chat) — **plus** our
differentiator: it also runs the **backend** (Python / Go / Node / any), seeds **mock
data**, and **auto‑wires API ↔ UI** so the whole app works end‑to‑end, automatically.

This plan is written so the system handles **every repo shape and every gap with one
mechanism**, not bespoke per‑repo code.

---

## 0. The core idea — one spec, one engine, self‑healing

The "easy way to do all of this properly" is to stop special‑casing repos and instead
normalise every repo into a single declarative artifact, then run *one* engine over it:

```
   any repo ──► RESOLVER ──► Preview Environment Spec (PES) ──► ENGINE ──► live env
                  (3 tiers)        (one normalized IR)         (uniform)        │
                                                                               ▼
                                                        VERIFY & SELF‑HEAL loop
                                              (bring up, detect reality, auto‑fix, retry)
```

- **PES** — a normalized internal representation of *any* environment (frontend(s),
  backend(s), datastore(s), mocks, wiring, routes, data seed, lifecycle). §2.
- **Resolver** — turns any repo into a PES via three tiers: **explicit → detected →
  AI‑synthesized**, with deterministic precedence. §3.
- **Engine** — materialises *any* PES into running, auth‑gated, wired, seeded K8s. One code
  path for FE‑only, BE‑only, full‑stack, monorepo, compose, etc. §5.
- **Verify & Self‑Heal** — brings each service up, compares reality to the spec, and
  auto‑corrects the common failure classes (then escalates to an agent that patches the
  PES). This is what makes heterogeneous, imperfectly‑detected repos "just work." §6.

**Adding a new framework or scenario = a new resolver rule or runtime profile** (data),
never new orchestration logic. That is the property that keeps "all scenarios" tractable.

---

## 1. Reuse vs. must‑fix (grounded in the current codebase)

### Already exists — reuse
| Capability | Where |
|---|---|
| `spin_preview_pod` → Deployment(dev‑server+editor‑bridge) + Service + Istio VS + git‑clone init | `src/devai/pipeline/stages/preview.py`, `src/devai/runtime/job_spec.py:235‑434`, `runtime/k8s_client.py apply_preview()` |
| Dashboard Preview tab + iframe (`PreviewPane`) reading `run.context.preview_url` | `dashboard/src/app/runs/[id]/page.tsx` |
| `app-scaffold.yaml` chains `scan → … → spin_preview_pod → post_report` | `blueprints/app-scaffold.yaml` |
| Tech detection (`detected_tech_stack`, `skill_profile_name`) | `src/devai/agents/tech_detector.py` |
| Editor‑bridge sidecar image | `ghcr.io/tesserix/devai-editor-bridge` |
| `*.tesserix.app` wildcard TLS, `tesseract-gateway`, `devai-auth-bff` reverse‑proxy, `devai` ns auth‑exempt | `tesserix-k8s/.../istio-config`, `charts/apps/devai-auth-bff` |
| Knative + KEDA scale‑to‑zero; Spot pool `optimized-v2` | cluster |

### Must‑fix gaps (each mapped to a mechanism in §15)
1. **Unauthenticated** — generated VS routes directly to the preview Service, bypassing the BFF.
2. **TLS/domain** — prod `previewDomain=devai.tesserix.app` (2nd‑level, not covered by `*.tesserix.app`) + VS gateway `devai-gateway` (live = `tesseract-gateway`).
3. **Ephemeral workspace** — `emptyDir`; needs a **PVC**.
4. **No backend / mock data / API↔UI wiring** — only runs an FE dev server.
5. **Detection unused** — hardcodes Node + `npm run dev`.
6. **No chat‑side preview / edit→reload loop.**
7. **No persistence/lifecycle** — `sandbox_pod`/`dev_server_port` unused; no `preview_sessions`, no GC.

---

## 2. The Preview Environment Spec (PES)

The single normalized IR. Everything downstream consumes only this. (Stored per session;
also expressible by users in `.platform/devai.yaml` under `preview:`.)

```yaml
version: 1
repo: tesserix/acme            # or a monorepo subpath set
ref: main
owner: user@tesserix.app
mode: detected                 # explicit | detected | synthesized
services:
  - name: web
    role: frontend             # frontend | backend | database | cache | queue | mock | worker | static
    workdir: apps/web          # monorepo subpath ("" = repo root)
    image: devai-preview-node:20
    install: ["pnpm install --frozen-lockfile"]
    run:     ["pnpm dev --host 0.0.0.0 --port 3000"]
    port: 3000
    hotReload: hmr             # hmr | reload | restart | none
    env: { NEXT_PUBLIC_API_URL: "${service.api.url}" }   # ${...} resolved by wiring
    healthcheck: { type: http, path: /, port: 3000, timeoutSec: 120 }
    dependsOn: [api]
  - name: api
    role: backend
    workdir: apps/api
    image: devai-preview-python:3.12
    install: ["uv sync"]
    run:     ["uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"]
    port: 8000
    hotReload: reload
    env: { DATABASE_URL: "${service.db.url}", CORS_ORIGINS: "${service.web.publicUrl}" }
    healthcheck: { type: http, path: /healthz, port: 8000, timeoutSec: 120 }
    dependsOn: [db]
  - name: db
    role: database
    engine: postgres           # postgres | mysql | mongo | sqlite | redis | nats | kafka
    image: postgres:16-alpine
    port: 5432
    data:                      # see §8
      migrate: auto            # auto | command | none
      seed: generate           # generate | fixtures | none | command
workspace: { pvc: true, sizeGi: 10, mountPath: /work }
routes:                        # see §9 — always via the BFF, never direct
  - host: "preview-${session}.tesserix.app" -> service: web  ; auth: devai_session ; ws: true
  - host: "api-${session}.tesserix.app"     -> service: api  ; auth: devai_session ; ws: true
mocks: []                      # external‑dependency stubs (Prism/recorded) — §8
secrets: mock                  # mock | none  (never real prod secrets)
lifecycle: { ttlMinutes: 240, idleScaleToZero: true, reuse: true }
resources: { memoryProfile: standard }   # memory‑only requests per platform policy
```

Design notes: `${service.X.url}` / `${X.publicUrl}` are resolved by the wiring pass (§7);
`role` drives both the runtime profile and the routing/seed behavior; one schema spans
FE‑only, BE‑only, full‑stack, multi‑service, and compose.

---

## 3. The Resolver — any repo → a PES (3 tiers, deterministic precedence)

Precedence (highest wins; tiers **merge**, they don't replace blindly):

**Tier 1 — Explicit (trust the repo).** If present, use directly:
- `.platform/devai.yaml` `preview:` block (our first‑class format → 1:1 to PES).
- `docker-compose.yml` / `compose.yaml` → map services (image/build, ports, env, depends_on) → PES services.
- `devcontainer.json` (+ `postCreateCommand`/`forwardPorts`).
- `Procfile`, `Makefile` (`make dev`), `Taskfile`, root `package.json` scripts (`dev`/`start`).

**Tier 2 — Detected (rules over signals).** `tech_detector` + a **signal→profile** ruleset
(§4 + §14 matrix). Each signal (a file, a dep, a script) contributes services/commands.
Deterministic, cache‑able, explainable ("detected Vite because vite.config.ts + `dev` script").

**Tier 3 — Synthesized (AI fills the gaps).** When tiers 1–2 are ambiguous/incomplete, the
**preview agent** proposes the missing PES fields from the repo tree + key files, then the
result is **verified** (§6) before trust. Used for odd stacks, missing scripts, or
multi‑service inference. Always shows the user what it inferred.

Resolver output is the PES + a **provenance** map (which tier/rule set each field) so the UI
can explain and the user can override any field.

---

## 4. Runtime profiles (per role/stack) — data, not code

| Role | Stack | Image | Install | Run (hot reload) | Port |
|---|---|---|---|---|---|
| FE | Next.js | `devai-preview-node` | pnpm/npm/yarn i | `next dev -H 0.0.0.0 -p 3000` (HMR) | 3000 |
| FE | Vite/React/Vue/Svelte | node | install | `vite --host --port 3000` (HMR) | 3000 |
| FE | Astro / SvelteKit / Nuxt | node | install | framework `dev` (HMR) | varies |
| FE | CRA / Webpack | node | install | `start` (WDS) | 3000 |
| FE | Static (Hugo/Jekyll/MkDocs/Docusaurus) | tool image | — | `serve`/`--watch` | varies |
| BE | FastAPI/Flask | `devai-preview-python` | uv/pip | `uvicorn … --reload` / `flask --debug run` | 8000 |
| BE | Django | python | pip | `manage.py runserver 0:8000` (+migrate) | 8000 |
| BE | Go/Gin/Echo | `devai-preview-go` | `go mod download` | `air` (live reload) | 8080 |
| BE | Node/Express/Nest | node | install | `tsx watch` / `nest start --watch` | 8080 |
| BE | Rails / Spring / .NET | stack image | bundle/gradle/dotnet restore | `rails s` / `bootRun` / `watch run` | varies |
| DB | Postgres/MySQL/Mongo | official alpine | — | seeded on start (§8) | std |
| Cache/Queue | Redis/NATS/Kafka | official | — | ephemeral, no persistence | std |
| Mock | REST/GraphQL | `stoplight/prism` / mock | — | serve spec/fixtures | — |

Per‑stack **prebaked dev images** (toolchain + warm package caches) cut cold start; base
images are the fallback. Profiles live in a registry table; new stack = new row.

---

## 5. The Engine — PES → running environment (one uniform path)

1. **Plan topology:** simple (≤1 FE + ≤1 BE + ≤1 DB) → **one pod, multiple containers**
   (shared `localhost`, one PVC). Complex/compose/multi‑FE → **per‑session sub‑namespace**
   with one Deployment+Service per service (still one PVC for source, separate data
   volumes). Same builder either way.
2. **Materialise:** for each PES service emit container spec (image/install+run as an
   entrypoint that `cd workdir && install && run`), readiness probe, ports, resources
   (memory‑only). Add the **git‑clone init** (existing) into the PVC, the **editor‑bridge**,
   and **seed/migrate init** for DB roles.
3. **Wire** (§7): resolve `${service.*.url}` → write FE env file(s) + BE CORS env before FE
   start; order via `dependsOn` + readiness gates.
4. **Route** (§9): create Services + a VirtualService whose destination is **`devai-auth-bff`**
   (not the service), and register the host→service+owner mapping with the BFF.
5. **Persist** a `preview_sessions` row; stamp `fe_url/api_url/editor_url` to `run.context`.
6. Hand to **Verify & Self‑Heal**.

Reuses the existing `build_preview_manifests()` (extended for N containers + PVC + seed +
BFF routing). The PES → manifest translation is the only stack‑agnostic glue.

---

## 6. Verify & Self‑Heal loop — *how it does all of this "properly"*

After materialise, the engine watches each service and reconciles reality vs. PES. This is
the difference between a demo and something that works across messy real repos.

For each service: wait on readiness; tail logs; classify outcome. On failure, apply the
matching **automated remediation**, then retry (bounded, e.g. 3 attempts); if still failing,
escalate to the **preview agent** to patch the PES; if that fails, surface a precise error.

| Failure class | Signal | Auto‑remediation |
|---|---|---|
| Install failed | non‑zero install, lockfile mismatch | switch pkg‑manager (pnpm↔npm↔yarn), clear cache, retry; for py try `pip` if `uv` absent |
| Wrong port | listening port ≠ PES port (from logs) | re‑read actual port from stdout, patch Service/probe/route |
| Never binds | readiness timeout, no listen | check run command vs scripts; agent re‑derives `run` |
| Missing env | crash referencing an env var | inject a mock/placeholder value; record needed‑secret |
| Migration/seed failed | ORM error | recompute seed against actual schema; fall back to fixtures |
| CORS blocked | browser console / 403 from FE→BE | patch BE CORS env to FE origin |
| OOM | OOMKilled | bump memory profile one tier, retry |
| Build required | dev server expects a build | run build step first, then serve |
| Monorepo wrong dir | no package at workdir | re‑detect workdir from lockfile location |

Every transition streams to the dashboard as a state (`cloning → installing → migrating →
seeding → wiring → starting → live` / `needs‑input`). The loop is what lets imperfect
detection still converge.

---

## 7. Auto API ↔ UI wiring (all conventions)

The wiring pass (deterministic, before FE start):

- **FE API‑base detection (ranked):** explicit PES env > `NEXT_PUBLIC_API_URL` /
  `VITE_API_URL`/`VITE_API_BASE` / `REACT_APP_API_URL` / `NUXT_PUBLIC_*` > a scanned
  `src/lib/api*`/`apiBase` constant > a Vite/Next **dev proxy** rewrite (`/api → BE`).
  Write the framework's dev env file (`.env.local`, `.env.development`) in the workspace.
- **Same‑pod vs split:** same‑pod CSR → `http://localhost:8000`; SSR or split → the public
  `api-<sess>.tesserix.app` (so the browser can reach it). Pick per call‑site (server vs
  client) where the framework distinguishes.
- **Backend:** bind `0.0.0.0`; set CORS to the FE origin (env or a tiny dev CORS shim);
  honor a `basePath`/prefix if detected.
- **Monorepo / split‑repo:** wire across subpaths, or across two repos by pinning both into
  one PES (FE service + BE service from a second `repo:`).
- All wiring writes **dev‑only files** (never product code) → nothing leaks unless the user
  saves (§10).

---

## 8. Mock data & external dependencies (every data scenario)

Resolver picks a **data plan** per backend (overridable):

- **DB‑backed (ORM detected):** ephemeral DB (Postgres/MySQL/Mongo/sqlite) → run the app's
  **migrations**/`AutoMigrate`/`prisma migrate` → **generate seed** (schema‑aware,
  referential‑integrity‑preserving, Faker‑style) cached in the PVC. Fallback: fixtures.
- **Spec‑first (OpenAPI/GraphQL SDL):** **Prism**/GraphQL mock serves realistic responses;
  can front the real BE for not‑yet‑implemented routes.
- **gRPC:** a reflection‑based mock or recorded responses.
- **External 3rd‑party APIs (Stripe, etc.):** route to a mock/stub (Prism from their spec)
  or recorded fixtures; never call real external services from a preview.
- **Cache/queue (Redis/NATS/Kafka):** ephemeral instance, no seed needed.
- **Auth‑gated apps:** seed a **mock user/session** (or a dev bypass flag) so protected
  flows are explorable.
- **Secrets:** all secret‑shaped env get **mock placeholders**; real GCP‑SM secrets are
  never mounted into previews (security, §9).

The seed/mocks are LLM‑assisted but deterministic and cached, regenerable on demand.

---

## 9. Auth, routing, isolation, security

- **Domains:** `preview-<sess>.tesserix.app`, `api-<sess>.tesserix.app` (single‑level →
  covered by `*.tesserix.app`); fix `previewDomain` and VS gateway → `tesseract-gateway`.
- **Auth via BFF only:** the VirtualService points at **`devai-auth-bff`**; the BFF
  validates `devai_session`, checks **owner == session email** (or admin), then proxies to
  the session's Service. Reuses the kagent/aregistry proxy pattern. **WebSocket/HMR**:
  pass `Upgrade`/`Connection` (verify in a Phase‑1 spike).
- **Isolation:** dedicated `devai-previews` namespace; **deny‑all egress** NetworkPolicy
  except package registries + the in‑env DB; non‑root; read‑only rootfs where possible; no
  access to prod data stores or real secrets; per‑session resource quota.
- **Blast radius:** previews can never reach prod namespaces; one preview can't see another
  (per‑session labels + NetworkPolicy).

---

## 10. Workspace, edit → reload, save‑to‑git

- **PVC per session** mounted by all containers at `/work`.
- **Edit path:** chat → `devai-api` → editor‑bridge writes file(s) → FE HMR / BE `--reload`
  picks it up → iframe live. A `POST /preview/{id}/reload` + a WS forces an iframe refresh
  when HMR can't (config changes).
- **Save:** editor‑bridge/devai‑api commits `/work` to shadow branch `preview/<sess>` →
  **"Open PR"** (reuses SCM adapter + the checkpoint/rollback timeline). `main` is never
  touched without explicit save.

---

## 11. Lifecycle, cost, reuse, GC

- **Create on demand**; **reuse** a live session for (repo, ref, owner) — this is the
  "repo already declares how to run → just run it" fast path.
- **Idle scale‑to‑zero** (Knative for stateless FE/BE) + a **reaper** CronJob deleting
  sessions idle > N min (last‑access tracked at the BFF) and a hard TTL (e.g. 4h).
- **Warm pool** of generic node/python dev pods + **PVC‑cached deps** + prebaked images →
  fast cold start.
- **Quotas:** per‑user max concurrent previews; `devai-previews` ResourceQuota; Spot pool;
  memory‑only requests.

---

## 12. Dashboard UX

- **Preview tab** → full‑stack **environment view**: FE iframe + status strip (web ●, api
  ●, db ●), service URLs, a **logs/errors drawer** (WS feed), and the live state machine
  (cloning→…→live / needs‑input).
- **Chat ↔ preview split** (new): `flex` + `min-w-0` + `var(--border)` divider (the
  `repo-panel.tsx` pattern); chat left, live preview right; chat edit → `reload`.
- **Entry points:** "Open preview" on Repos rows, Workflows, and the run‑detail tab.
- **Provenance + overrides:** show what the resolver inferred; let the user edit any PES
  field inline and re‑run.

---

## 13. Backend API + controller + data model

- `PreviewService` drives Resolver → Engine → Verify loop.
- Endpoints (mounted like other routers): `POST /api/preview/start {repo,ref,mode?}`,
  `GET /api/preview/{id}`, `POST /api/preview/{id}/reload|save|stop`,
  `GET /api/preview/{id}/logs` (SSE/WS), `GET /api/preview/{id}/spec` (the PES + provenance).
- `preview_sessions` table (schema in `tesserix-k8s` db‑schema‑bootstrap): `id, repo, ref,
  owner, namespace, pes jsonb, fe_url, api_url, editor_url, services jsonb, status,
  last_access_at, expires_at, created_at`.
- A **reaper** (CronJob/in‑process loop) for TTL/idle GC.

---

## 14. Scenario coverage matrix (every repo shape → PES)

| Scenario | Resolver tier | PES shape |
|---|---|---|
| Next.js SSR app | detected | 1 FE (next dev) |
| Vite/React/Vue/Svelte SPA | detected | 1 FE |
| Static (Hugo/Docusaurus) | detected | 1 static FE |
| Backend‑only API (FastAPI/Go/Express) | detected | 1 BE + (DB if ORM) + Swagger/Prism for "UI" |
| Full‑stack monorepo (apps/web + apps/api) | detected (subpaths) | FE + BE + DB, wired |
| Full‑stack split repos | explicit/synth | FE(repoA) + BE(repoB) + DB, wired |
| `docker-compose.yml` present | explicit | services mapped 1:1 |
| `devcontainer.json` present | explicit | devcontainer + forwardPorts |
| Needs DB | detected | + database role + migrate/seed |
| Needs cache/queue | detected | + redis/nats/kafka role |
| Calls 3rd‑party API | detected/synth | + mock service (Prism) |
| Auth‑gated app | synth | + seeded mock user / dev bypass |
| Build‑required app | detected | run = build → serve |
| Blank/new repo | scaffold first | app‑scaffold blueprint → then PES |
| Undetectable | synth → needs‑input | partial PES + prompt user for `run`/`port` |

## 15. Gap‑closure table (every gap → mechanism)

| Gap (§1) | Closed by |
|---|---|
| Unauthenticated | §9 BFF‑only routing + owner check |
| TLS/domain | §9 single‑level host + `tesseract-gateway` + `previewDomain=tesserix.app` |
| Ephemeral workspace | §10 PVC per session |
| No backend/mock/wiring | §2 PES roles + §5 engine + §7 wiring + §8 data |
| Detection unused | §3 resolver + §4 profiles |
| No chat preview / reload | §10 edit→reload + §12 chat split |
| No persistence/lifecycle | §11 lifecycle + §13 sessions/reaper |
| Heterogeneous/odd repos | §3 tier‑3 synth + §6 self‑heal |
| Cold start | §11 prebaked images + warm pool + PVC cache |
| Failures (install/port/OOM/CORS/migrate) | §6 self‑heal classes |

---

## 16. Phased rollout (with acceptance criteria)

- **Phase 0 — Spike (1–2 days):** confirm WebSocket/HMR passes through the BFF; PVC
  read‑write‑once works across the container set. *Accept:* a hand‑written Vite PES runs and
  hot‑reloads through `preview-x.tesserix.app` behind `devai_session`.
- **Phase 1 — FE preview real in prod:** PES (FE‑only) + engine + BFF routing + PVC +
  domain/gateway fix + `preview_sessions` + `/api/preview/*` + dashboard wired. *Accept:*
  any detected‑FE repo previews, auth‑gated, persistent, from the Preview tab.
- **Phase 2 — full‑stack (differentiator):** BE + DB roles, runtime profiles, resolver tiers
  1–2, API↔UI wiring, mock data, verify & self‑heal core classes. *Accept:* a FastAPI+Next
  monorepo comes up end‑to‑end with seeded data and a working UI→API, no manual config.
- **Phase 3 — chat hot‑edit loop:** chat↔preview split, editor‑bridge edit→HMR, reload WS,
  save‑to‑git/PR. *Accept:* a chat edit changes the live preview within seconds; "Open PR"
  produces a shadow‑branch PR.
- **Phase 4 — scale & breadth:** tier‑3 synth, compose/multi‑service, logs feed, idle
  scale‑to‑zero + reaper, quotas, prebaked images, provenance/override UI. *Accept:* compose
  repos and odd stacks resolve; idle previews scale to zero; per‑user quota enforced.

## 17. Risks, limits, open decisions

- **Cost/sprawl** → TTL + idle‑zero + quota + Spot. **Security** → `devai-previews` ns,
  deny‑all egress, non‑root, mock secrets, owner‑scoped BFF. **Cold start** → prebaked +
  warm pool + cache. **HMR via proxy** → Phase‑0 spike. **Repo heterogeneity** → explicit
  override always wins; synth + self‑heal; a clear `needs‑input` state when truly unknown.
- **Open decisions to confirm:** (a) topology default — single multi‑container pod vs
  per‑service pods for the common case; (b) PES authoring surface in `.platform/devai.yaml`
  vs UI‑only; (c) Knative vs plain Deployment+KEDA for previews; (d) max concurrent
  previews per user; (e) default TTL.

## 18. First concrete tasks (Phase 0/1)

1. `devai` `job_spec.py`: host → `preview-<id>.tesserix.app`, gateway → `tesseract-gateway`,
   VS destination → `devai-auth-bff`, add PVC volume; emit from a (Phase‑1 FE‑only) PES.
2. `devai-auth-bff`: dynamic `preview-*`/`api-*` host proxy with `devai_session` + owner
   check + WebSocket passthrough.
3. `devai-api`: `PreviewService` (Resolver→Engine→Verify) + `/api/preview/*` +
   `preview_sessions` accessor.
4. `tesserix-k8s`: `devai-previews` namespace (quota, deny‑all‑egress NetworkPolicy),
   `previewDomain=tesserix.app`, PVC StorageClass, `preview_sessions` schema.
5. `dashboard`: Preview tab + chat split wired to `/api/preview/*` + reload WS.
