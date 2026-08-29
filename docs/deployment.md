# DevAI — Deployment & Operations Guide

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │            GKE Cluster               │
                    │       tesseract-prod-in-gke          │
                    │          asia-south1                 │
                    │                                     │
                    │  ┌─────────────┐  ┌──────────────┐ │
                    │  │   devai ns   │  │  nats ns     │ │
                    │  │             │  │  (3 replicas) │ │
GitHub Webhooks ───►│  │  DevAI Pod  │◄─┤  JetStream   │ │
                    │  │  (FastAPI + │  └──────────────┘ │
Dashboard ─────────►│  │   Agents)  │                    │
                    │  │             │  ┌──────────────┐ │
CLI ───────────────►│  │             │◄─┤ redis-devai  │ │
                    │  └─────────────┘  │  (1 replica) │ │
                    │                    └──────────────┘ │
                    └─────────────────────────────────────┘
```

## Infrastructure

| Component | Namespace | Service URL | Notes |
|-----------|-----------|-------------|-------|
| DevAI API + Agents | `devai` | `devai.devai.svc.cluster.local:8080` | Single deployment, all agents |
| NATS JetStream | `nats` | `nats.nats.svc.cluster.local:4222` | Shared with other services, 3 replicas |
| Redis | `redis-devai` | `redis.redis-devai.svc.cluster.local:6379` | Dedicated, no auth, 2Gi PVC |
| Dashboard | — | `https://devai.tesserix.app` | Istio VirtualService |
| Keycloak (auth) | `identity-internal` | `https://internal-identity.tesserix.app` | Internal realm, OIDC |

## GCP Resources

| Resource | Name | Purpose |
|----------|------|---------|
| GCP Project | `tesseracthub-480811` | All resources |
| GCP SA | `app-secrets-devai-prod@tesseracthub-480811.iam.gserviceaccount.com` | Pod identity for secrets |
| WIF Binding | `tesseracthub-480811.svc.id.goog[devai/devai]` | K8s SA → GCP SA |
| WIF (CI) | `github-actions@tesseracthub-480811.iam.gserviceaccount.com` | GitHub Actions → GKE |

## GCP Secret Manager Secrets

| Secret Name | Purpose | Populated? |
|-------------|---------|------------|
| `prod-devai-openai-api-key` | OpenAI API key for Codex agents | **TODO** |
| `prod-devai-anthropic-api-key` | Anthropic API key for Claude agents | **TODO** |
| `prod-devai-github-app-id` | GitHub App ID | **TODO** |
| `prod-devai-github-app-private-key` | GitHub App private key (PEM) | **TODO** |
| `prod-devai-github-app-installation-id` | GitHub App installation ID | **TODO** |
| `prod-devai-github-webhook-secret` | Webhook HMAC secret | **TODO** |
| `prod-devai-github-oauth-client-id` | OAuth Client ID (dashboard login) | **TODO** |
| `prod-devai-github-oauth-client-secret` | OAuth Client Secret | **TODO** |

### Populating Secrets

```bash
# Example: set the Anthropic API key
echo -n "sk-ant-api03-..." | gcloud secrets versions add prod-devai-anthropic-api-key \
  --project=tesseracthub-480811 --data-file=-

# Example: set the GitHub App private key (from PEM file)
gcloud secrets versions add prod-devai-github-app-private-key \
  --project=tesseracthub-480811 --data-file=devai-app.private-key.pem
```

## GitHub App Setup

1. Go to **https://github.com/organizations/tesserix/settings/apps/new**
2. Create app with these settings:
   - **Name:** DevAI Pipeline
   - **Homepage:** https://devai.tesserix.app
   - **Callback URL:** https://devai.tesserix.app/dashboard/auth/callback
   - **Webhook URL:** https://devai.tesserix.app/webhook/github
   - **Webhook secret:** Generate and save to GCP Secret Manager
   - **Permissions:**
     - Issues: Read & Write
     - Pull requests: Read & Write
     - Contents: Read & Write
     - Projects: Admin
     - Checks: Read & Write
     - Metadata: Read-only
   - **Events:** issues, pull_request, projects_v2_item
3. After creation:
   - Note the **App ID** → save to `prod-devai-github-app-id`
   - Generate a **private key** → save to `prod-devai-github-app-private-key`
   - **Install** the app on the `tesserix` organization
   - Note the **Installation ID** from the URL → save to `prod-devai-github-app-installation-id`
   - Go to **OAuth** section, note **Client ID** and **Client Secret** → save to respective GCP secrets

## Keycloak OIDC Setup (Dashboard Auth)

The dashboard authenticates via the **internal Keycloak** at `internal-identity.tesserix.app` (realm: `tesserix-internal`).

### Create the Keycloak Client

1. Log in to Keycloak admin: `https://internal-identity.tesserix.app/admin/`
2. Select realm: **tesserix-internal**
3. Go to **Clients** → **Create client**
4. Settings:
   - **Client ID:** `devai-dashboard`
   - **Client Protocol:** openid-connect
   - **Access Type:** confidential
   - **Valid Redirect URIs:**
     - `https://devai.tesserix.app/dashboard/auth/callback`
     - `http://localhost:8080/dashboard/auth/callback` (for local dev)
   - **Web Origins:** `+`
5. Go to **Credentials** tab → copy the **Client Secret**
6. Save to GCP:
   ```bash
   echo -n "<client-secret>" | gcloud secrets versions add prod-devai-keycloak-client-secret \
     --project=tesseracthub-480811 --data-file=-
   ```

### Identity URL

- **Public URL:** `https://internal-identity.tesserix.app`
- **Internal URL:** `keycloak.identity-internal.svc.cluster.local:8080`
- **Realm:** `tesserix-internal`
- **VirtualService:** Added in `tesserix-k8s/charts/thirdparty/istio-config/values-prod.yaml` (internalIdentityAliases)

### Auth Flow

```
User → devai.tesserix.app/dashboard/auth/login
  → Redirect to internal-identity.tesserix.app/realms/tesserix-internal/protocol/openid-connect/auth
  → Keycloak login form
  → Callback to devai.tesserix.app/dashboard/auth/callback
  → Session stored in Redis, cookie set
  → Redirect to /dashboard
```

## ArgoCD Manifests (in tesserix-k8s)

| File | Purpose |
|------|---------|
| `argocd/prod/infrastructure/redis-devai.yaml` | Redis ArgoCD Application |
| `argocd/prod/apps/devai-app-of-apps.yaml` | DevAI app-of-apps meta-Application |
| `argocd/prod/apps/devai/devai.yaml` | DevAI service ArgoCD Application |
| `argocd/prod/infrastructure/kustomization.yaml` | References redis-devai |
| `argocd/prod/apps/kustomization.yaml` | References devai-app-of-apps |

## Helm Chart (in tesserix-k8s)

The prod Helm chart (Chart.yaml, values, deployment/service/serviceaccount
templates, ExternalSecret, VirtualService for devai.tesserix.app) lives in
the `tesserix-k8s` repo, not here. This repo carries only the local/sandbox
chart at `k8s/chart/`, used by `sandboxctl` for kind-based development.

## CI/CD Pipeline

### On Push to main

1. **CI** (`.github/workflows/ci.yaml`): Lint → Type check → Unit tests
2. **CD** (`.github/workflows/cd.yaml`):
   - Build Docker image
   - Push to GHCR: `ghcr.io/tesserix/devai/devai:<sha>`
   - Tag as `latest`
   - GKE auth via WIF
   - Restart deployment
   - Trivy security scan

### Build Cycle (Limited CI Minutes)

```bash
# 1. Make public
gh repo edit tesserix/devai --visibility public --accept-visibility-change-consequences

# 2. Push (triggers CI/CD)
git push origin main

# 3. Monitor
gh run list --repo tesserix/devai --limit 3
gh run view <run-id> --repo tesserix/devai

# 4. Make private (after CI is green)
gh repo edit tesserix/devai --visibility private --accept-visibility-change-consequences
```

## Local Development

```bash
# Install
pip install -e ".[dev]"

# Port-forward NATS and Redis from GKE
kubectl port-forward svc/nats -n nats 4222:4222 &
kubectl port-forward svc/redis -n redis-devai 6379:6379 &

# Set environment
cp .env.example .env
# Edit .env with your API keys

# Run all agents + webhook server
python -m devai serve

# Or trigger a pipeline via CLI
python -m devai run --repo tesserix/test-app --requirements "Add a health check endpoint"

# Check status
python -m devai status
```

## Monitoring

- **Dashboard:** https://devai.tesserix.app/dashboard
- **Health check:** https://devai.tesserix.app/healthz
- **Readiness:** https://devai.tesserix.app/readyz
- **Logs:** `kubectl logs -f deployment/devai -n devai`
- **NATS monitoring:** `kubectl port-forward svc/nats -n nats 8222:8222` → http://localhost:8222

## Troubleshooting

| Issue | Fix |
|-------|-----|
| ExternalSecret not syncing | `kubectl delete secret devai-secrets -n devai` → ESO recreates |
| NATS connection refused | Check Istio port exclusion: `traffic.sidecar.istio.io/excludeOutboundPorts: "4222,6379"` |
| Redis connection error | Verify Redis pod: `kubectl get pods -n redis-devai` |
| GitHub webhook 401 | Verify webhook secret matches `prod-devai-github-webhook-secret` |
| Dashboard OAuth failure | Check `prod-devai-github-oauth-client-id` and callback URL |
