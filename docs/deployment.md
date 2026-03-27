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

## ArgoCD Manifests (in tesserix-k8s)

| File | Purpose |
|------|---------|
| `argocd/prod/infrastructure/redis-devai.yaml` | Redis ArgoCD Application |
| `argocd/prod/apps/devai-app-of-apps.yaml` | DevAI app-of-apps meta-Application |
| `argocd/prod/apps/devai/devai.yaml` | DevAI service ArgoCD Application |
| `argocd/prod/infrastructure/kustomization.yaml` | References redis-devai |
| `argocd/prod/apps/kustomization.yaml` | References devai-app-of-apps |

## Helm Chart (in devai repo)

```
helm/devai/
├── Chart.yaml
├── values.yaml          # Base config (NATS URL, Redis URL, ports)
├── values-prod.yaml     # Prod overrides (SA annotation, resources)
└── templates/
    ├── deployment.yaml         # Pod spec with probes, annotations
    ├── service.yaml            # ClusterIP:8080
    ├── serviceaccount.yaml     # With WIF annotation
    ├── configmap.yaml          # Non-secret env vars
    ├── externalsecret.yaml     # Maps GCP secrets → K8s Secret
    └── virtualservice.yaml     # Istio routing for devai.tesserix.app
```

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
