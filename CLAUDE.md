# Claude Reference Guide — Tesserix Workspace

This is the parent directory for all Tesserix repositories.
For detailed conventions, see `tesserix-k8s/CLAUDE.md`.

---

## Critical Rules

### 1. Git Identity

**Always** configure before any commit or push:

```bash
git config user.name "sam123ben"
git config user.email "samyak.rout@gmail.com"
```

### 2. No AI References

**NEVER** include Claude, Copilot, Anthropic, AI tool, or `Co-Authored-By` mentions in:
- Commit messages
- PR titles or descriptions
- Code comments
- Changelog entries
- Any file content

### 3. CI Build & Deploy Procedure (MUST FOLLOW)

The `tesserix` GitHub org has **limited Actions minutes** for private repos.
Every CI build requires a **public→build→private** cycle:

```bash
# Step 1: Make repo public
gh repo edit tesserix/<repo> --visibility public --accept-visibility-change-consequences

# Step 2: Push code (triggers CI automatically) OR trigger manually
git push origin main
# OR
gh workflow run ci.yml --repo tesserix/<repo> --ref main

# Step 3: Wait for CI to complete — check status
gh run list --repo tesserix/<repo> --limit 3
gh run view <run-id> --repo tesserix/<repo>

# Step 4: Once CI passes (all green), make repo private again
gh repo edit tesserix/<repo> --visibility private --accept-visibility-change-consequences
```

**Important:**
- Always verify CI is fully complete before making private
- If CI fails, fix the issue, push again (repo stays public), wait for green, then make private
- Never leave repos public overnight — always toggle back

### 4. @tesserix/web Private Package Token

All Next.js apps depend on `@tesserix/web` from GitHub Packages. Install locally with:

```bash
NODE_AUTH_TOKEN=$(gcloud secrets versions access latest --secret=prod-ghcr-token --project=tesseracthub-480811) npm install
```

The token `prod-ghcr-token` in GCP Secret Manager is shared across all repos.
In CI, it's set as the `PKG_READ_TOKEN` GitHub Actions secret on each repo.

### 5. GCP & GitHub

- **GCP Project:** `tesseracthub-480811`
- **GCP Region:** `asia-south1`
- **GKE Cluster:** `tesseract-prod-in-gke`
- **GitHub Org:** `tesserix` (all repos live here, not under `sam123ben`)

### 6. No Manual kubectl apply — ArgoCD Only

**NEVER** use `kubectl apply`, `kubectl create`, `kubectl patch`, `kubectl edit`, or `kubectl set` to deploy or modify cluster resources directly. All changes must go through ArgoCD:

1. Make changes in the `tesserix-k8s` repo (Helm charts, values, external-secrets, ArgoCD app definitions)
2. Commit and push to `main`
3. ArgoCD auto-syncs (or trigger manually: `kubectl patch app <name> -n argocd --type merge -p '{"operation":{"sync":{"syncStrategy":{"apply":{"force":false}}}}}'`)

**Why:** Manual applies drift from Git state, get overwritten by ArgoCD self-heal, and are not auditable. The only exception is emergency debugging (e.g., `kubectl logs`, `kubectl describe`, `kubectl exec` for read-only investigation).

**Key ArgoCD patterns:**
- **Helm charts:** `tesserix-k8s/charts/apps/<service>/` — templates + values
- **ArgoCD apps:** `tesserix-k8s/argocd/prod/apps/<project>/` — app-of-apps pattern
- **External Secrets:** `tesserix-k8s/external-secrets/prod/<namespace>/` — GHCR secrets, DB passwords via GCP Secret Manager
- **Istio config:** `tesserix-k8s/charts/thirdparty/istio-config/` — namespace labels, mTLS, gateway config
- **Namespace labels** (e.g., `istio-injection=enabled`): managed by `istio-config` chart, not manual `kubectl label`

---

## Repo Map

| Repo | Type | Stack | Dev Port |
|------|------|-------|----------|
| `tesserix-k8s` | Infrastructure | Helm, ArgoCD, Istio, KEDA | — |
| `go-shared` | Library | Go 1.26, shared packages | — |
| `design-system` | Library | React, Tailwind, @tesserix/web | — |
| `marketplace-admin` | Frontend | Next.js 16 | 3001 |
| `marketplace-storefront` | Frontend | Next.js 16 | 3200 |
| `marketplace-onboarding` | Frontend | Next.js 16 | 4201 |
| `tesserix-home` | Frontend | Next.js 16 | 3002 |
| `tesserix-blog` | Frontend | Next.js 16, MongoDB | 3003 |
| `marketplace-*-service` | Backend | Go 1.26, Gin, PostgreSQL | 8080+ |
| `*-service` | Backend | Go 1.26, Gin, PostgreSQL | 8080+ |
| `Home-Chef-App` | Monorepo | pnpm, React 19/Vite 6 + Go 1.25/Gin, Razorpay | 5173/8080 |

---

## HomeChef Platform (fe3dr.com)

- **GitHub Repo:** `tesserix/Home-Chef-App` (private, pnpm monorepo)
- **Local Path:** `Home-Chef-App/`
- **Apps:** `apps/web`, `apps/vendor-portal`, `apps/admin-portal`, `apps/delivery-portal`, `apps/api`
- **Frontend:** React 19, Vite 6, Tailwind CSS 4, Radix UI, React Router 7, TanStack Query, Zustand
- **Backend:** Go 1.25, Gin, GORM, PostgreSQL 16, Redis, NATS JetStream
- **Payments:** Razorpay Route (split payments)
- **CI/CD:** 7 GitHub Actions workflows → GHCR → GKE/ArgoCD

HomeChef is a food delivery platform deployed on GKE in the `homechef` namespace.

### Services (all Knative Serving)

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `homechef-api` | `ghcr.io/tesserix/home-chef-app/homechef-api` | 8080 | Go/Gin backend API |
| `homechef-web` | `ghcr.io/tesserix/home-chef-app/homechef-web` | 80 | Customer-facing SPA |
| `homechef-vendor-portal` | `ghcr.io/tesserix/home-chef-app/homechef-vendor-portal` | 80 | Chef/vendor dashboard |
| `homechef-admin-portal` | `ghcr.io/tesserix/home-chef-app/homechef-admin-portal` | 80 | Platform admin panel |
| `homechef-delivery-portal` | `ghcr.io/tesserix/home-chef-app/homechef-delivery-portal` | 80 | Delivery partner dashboard |
| `homechef-auth-bff` | `ghcr.io/tesseract-nexus/global-services/auth-bff` | 8090 | Auth Backend-for-Frontend |

### Domains (Istio VirtualServices)

| Domain | Target |
|--------|--------|
| `fe3dr.com` / `www.fe3dr.com` | homechef-web |
| `vendors.fe3dr.com` | homechef-vendor-portal |
| `admin.fe3dr.com` | homechef-admin-portal |
| `delivery.fe3dr.com` | homechef-delivery-portal |
| `api.fe3dr.com` | homechef-api |
| `identity.fe3dr.com` | Keycloak customer realm |
| `internal-identity.fe3dr.com` | Keycloak internal realm |

### Route Prefixes (on fe3dr.com)

- `/bff/` → auth-bff (customer realm)
- `/auth/` → Keycloak auth callback
- `/driver-bff/` → driver auth
- `/api/*` → API service
- `/ws/*` → WebSocket (3600s timeout)

### Infrastructure

- **PostgreSQL 16**: `postgresql.postgresql-homechef.svc.cluster.local:5432`, db `homechef_db`, 60Gi, 300 max connections
- **Redis**: `redis.redis-homechef.svc.cluster.local:6379`, 4Gi, auth enabled (session store)
- **Cloudflare Tunnel**: token from GCP Secret Manager (`prod-homechef-cloudflare-tunnel-token`)
- **External Secrets**: `homechef-api-secrets`, `homechef-auth-bff-secrets` synced from GCP SM
- **DB Bootstrap CronJob**: every 30min, idempotent schema provisioning
- **GCP SA**: `app-secrets-homechef-prod@tesseracthub-480811.iam.gserviceaccount.com`

### Auth (Dual Keycloak)

- **Customer realm (homechef)**: `identity.fe3dr.com` / `keycloak.identity-customer.svc.cluster.local:8080`
- **Internal realm (tesserix-internal)**: `internal-identity.fe3dr.com` / `keycloak.identity-internal.svc.cluster.local:8080`
- Admin portal uses internal realm; customer/vendor/delivery use customer realm

### Helm Charts

All under `tesserix-k8s/charts/apps/homechef-*` with ArgoCD apps in `tesserix-k8s/argocd/prod/apps/homechef/`

### E2E Tests (Playwright)

**Location:** `homechef-e2e-tests/`

**TEMPORARY** — E2E test users and this test project are for development/debugging only. Must be removed and cleaned up before production-ready release:
- Keycloak test user `e2e-test@fe3dr.com` in `homechef` realm (customer)
- Keycloak test user `e2e-admin@fe3dr.com` in `tesserix-internal` realm (admin)
- The `homechef-e2e-tests/` project directory

**Login flows per portal:**
- **Web** (`fe3dr.com`): Homepage → click "Login" → click "Sign in with email" → Keycloak form
- **Vendor** (`vendors.fe3dr.com`): Login page auto-loads → click "Sign in with Email" → Keycloak form
- **Admin** (`admin.fe3dr.com`): Navigate to `/bff/login` → redirects to internal Keycloak → form login (BFF uses HttpOnly cookies, no storageState)
- **Delivery** (`delivery.fe3dr.com`): Role selection ("I'm a Driver" / "I'm Staff") → email login → Keycloak form

**Run commands:**
```bash
cd homechef-e2e-tests
npm test                    # all tests headless
npm run test:headed         # all tests with browser
npm run test:web            # web only
npm run test:vendor         # vendor only
npm run test:admin          # admin only
npm run test:delivery       # delivery only
npm run setup               # auth setup only
```
