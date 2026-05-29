# DevAI — Local Kubernetes secrets

This directory holds the LOCAL-only Kubernetes secrets for running DevAI on a
kind cluster. Production delivers the same data via External Secrets Operator
from GCP Secret Manager (`tesserix-k8s/external-secrets/prod/devai/`) — none of
the files here are used in prod, and `k8s/secrets.yaml` is gitignored.

## Two local flows (pick one)

`k8s/secrets.example.yaml` covers both:

### 1. All-in-one chart (`sandboxctl deploy`)

Uses the bundled chart in `k8s/chart/` (single Deployment + bundled Postgres /
Redis via `datastores.yaml`) and the single **`devai-secrets`** Secret at the
top of `secrets.example.yaml`. `sandboxctl deploy` applies it automatically.

### 2. Per-chart tesserix-k8s charts (agentic-registry-style local overlay)

Deploys the individual charts from `tesserix-k8s/charts/apps/` with their
`values-local.yaml` overlays. This is the additive flow that mirrors what was
done for `agentic-registry`. It uses the **additional named Secrets** in the
lower half of `secrets.example.yaml`:

| Secret | Used by | Notes |
|--------|---------|-------|
| `devai-local-secrets` | `devai-postgres-local` chart | `POSTGRES_PASSWORD` |
| `postgresql-devai-password` | `devai-api`, `devai-sre` | key `password` — must equal `POSTGRES_PASSWORD` |
| `redis-devai-password` | `devai-api`, `devai-sre` | key `password` |
| `devai-api-secrets` | `devai-api`, `devai-sre` | LLM / SCM / auth keys |
| `devai-github-pat` | `devai-api` | key `token` |
| `devai-auth-bff-secrets` | `devai-auth-bff` | created here because its ExternalSecret is disabled locally |

## Quick start (flow 2)

```bash
# 1. Copy and fill in the template (stringData → plain text values)
cp k8s/secrets.example.yaml k8s/secrets.yaml
#    set a POSTGRES_PASSWORD (same value in devai-local-secrets AND
#    postgresql-devai-password) and any LLM keys you want to exercise.

# 2. Create the namespace and apply the secrets
kubectl create namespace devai --dry-run=client -o yaml | kubectl apply -f -
kubectl -n devai apply -f k8s/secrets.yaml

# 3. Build + load the images into kind (adjust Dockerfile paths)
docker build -t devai-api:local           -f Dockerfile .
docker build -t devai-sre:local           -f Dockerfile.sre .
docker build -t devai-dashboard:local     -f dashboard/Dockerfile dashboard
docker build -t devai-sre-dashboard:local -f sre-dashboard/Dockerfile sre-dashboard
docker build -t devai-auth-bff:local      -f services/auth-bff/Dockerfile services/auth-bff
for t in devai-api devai-sre devai-dashboard devai-sre-dashboard devai-auth-bff; do
  kind load docker-image $t:local
done

# 4. Deploy the bundled Postgres, then each service with its local overlay.
#    (Run a local Redis reachable at devai-redis-local:6379 if you need cache.)
K8S=/path/to/tesserix-k8s/charts/apps
helm upgrade --install devai-postgres-local  $K8S/devai-postgres-local  -n devai
helm upgrade --install devai-api             $K8S/devai-api             -n devai -f $K8S/devai-api/values-local.yaml
helm upgrade --install devai-sre             $K8S/devai-sre             -n devai -f $K8S/devai-sre/values-local.yaml
helm upgrade --install devai-dashboard       $K8S/devai-dashboard       -n devai -f $K8S/devai-dashboard/values-local.yaml
helm upgrade --install devai-sre-dashboard   $K8S/devai-sre-dashboard   -n devai -f $K8S/devai-sre-dashboard/values-local.yaml
helm upgrade --install devai-auth-bff        $K8S/devai-auth-bff        -n devai -f $K8S/devai-auth-bff/values-local.yaml

# 5. Port-forward what you want to use
kubectl -n devai port-forward svc/devai-api 8080:8080
kubectl -n devai port-forward svc/devai-dashboard 3100:3100
```

## Resetting local data

`devai-postgres-local` ships a suspended cleanup CronJob (never runs on a
schedule — trigger on demand):

```bash
# clean (default): truncate all public tables, keep schema
kubectl -n devai create job --from=cronjob/devai-postgres-local-cleanup clean-$(date +%s)
# wipe: drop + recreate the public schema (set cleanup.mode=wipe in values)
```

## Notes

- `k8s/secrets.yaml` is gitignored — never commit real secrets.
- `stringData` is base64-encoded by Kubernetes automatically; paste plain text.
- The `values-local.yaml` overlays disable External Secrets, Workload Identity,
  KEDA and (where present) Istio, and switch images to `:local` with
  `pullPolicy: IfNotPresent`. They never alter prod values.

## Prod source — GCP Secret Manager mapping

In prod, `tesserix-k8s/external-secrets/prod/devai/externalsecret.yaml` syncs the
`devai-api-secrets` / `postgresql-devai-password` / `redis-devai-password` /
`devai-github-pat` Secrets from GCP Secret Manager (project `tesseracthub-480811`):

| Local key | GCP SM secret |
|-----------|---------------|
| POSTGRES_PASSWORD (postgresql-devai-password/password) | prod-devai-postgresql-password |
| REDIS_PASSWORD (redis-devai-password/password) | prod-devai-redis-password |
| DEVAI_OPENAI_API_KEY | prod-devai-openai-api-key |
| DEVAI_ANTHROPIC_API_KEY | prod-devai-anthropic-api-key |
| DEVAI_GROQ_API_KEY | prod-devai-groq-api-key |
| DEVAI_GEMINI_API_KEY | prod-devai-gemini-api-key |
| DEVAI_GITHUB_APP_ID | prod-devai-github-app-id |
| DEVAI_GITHUB_APP_PRIVATE_KEY | prod-devai-github-app-private-key |
| DEVAI_GITHUB_APP_INSTALLATION_ID | prod-devai-github-app-installation-id |
| DEVAI_GITHUB_WEBHOOK_SECRET | prod-devai-github-webhook-secret |
| DEVAI_GITHUB_OAUTH_CLIENT_ID | prod-devai-github-oauth-client-id |
| DEVAI_GITHUB_OAUTH_CLIENT_SECRET | prod-devai-github-oauth-client-secret |
| DEVAI_KEYCLOAK_CLIENT_SECRET | prod-devai-keycloak-client-secret |
| DEVAI_LANGCHAIN_API_KEY | prod-devai-langsmith-api-key |
| DEVAI_CLOUDFLARE_API_TOKEN | prod-devai-cloudflare-api-token |
| DEVAI_CLOUDFLARE_ACCOUNT_ID | prod-devai-cloudflare-account-id |
| devai-github-pat/token | prod-devai-github-pat |
| aregistry-jwt/jwt-secret | prod-aregistry-jwt-secret |

Read a prod value: `gcloud secrets versions access latest --secret=<name> --project=tesseracthub-480811`
