# Local setup — DevAI on `sandboxctl`

Bring DevAI up on a local Kubernetes sandbox with
[`sandboxctl`](https://github.com/tesserix/sandboxctl) (kind + Argo CD + Istio +
in-cluster registry/Gitea, on `https://*.sandbox.app:8443`).

> DevAI is multi-service, and **`sandboxctl` deploys one chart = one app = one
> URL**. Deploy the shared `local-infra` datastores once, then each core
> service. `deploy` auto-applies `k8s/secrets.yaml` and auto-picks each chart's
> `values-local.yaml`.
>
> DevAI no longer runs its own local Postgres/Redis/NATS. It connects to the
> **shared `local-infra`** datastores at stable in-cluster DNS
> (`local-pg-rw.local-infra`, `redis.local-infra`, `nats.local-infra`,
> `mongodb.local-infra`). The `local-infra-creds` Secret is reflected into the
> `devai` namespace, so products auto-connect with the shared
> `local-sandbox-dev` password — no per-product DB to deploy.

## 0. Prerequisites (one time)

```sh
command -v sandboxctl >/dev/null || brew install tesserix/tap/sandboxctl
```

Charts live in `../tesserix-k8s/charts/apps` (sibling checkout). Shared
datastores: `local-infra` (CNPG Postgres + Redis + NATS + Mongo).

## 1. Fill in local secrets — IMPORTANT

```sh
cp k8s/secrets.example.yaml k8s/secrets.yaml      # gitignored
$EDITOR k8s/secrets.yaml
```

DevAI's deployments **hardcode prod secret names**, so `k8s/secrets.yaml` defines
several named Secrets (not one): `postgresql-devai-password`, `redis-devai-password`,
`devai-api-secrets`, `devai-github-pat`, plus the auth-bff secrets. **Keep
`postgresql-devai-password/password` and `redis-devai-password/password` equal to
the shared `local-infra` password (`local-sandbox-dev`)** — the same value the
reflected `local-infra-creds` Secret carries. The GCP Secret Manager source for
each key is tabulated in [`k8s/README.md`](k8s/README.md). `sandboxctl deploy`
applies the whole file to the namespace in one shot.

## 2. Bring the platform up (first run ≈ 10 min)

The CNPG operator backs the shared Postgres, so bring the sandbox up with
`--with-cnpg`:

```sh
sandboxctl up --with-cnpg --podman-disk 80 --podman-memory 12g
```

Then deploy the **shared `local-infra`** datastores once (Postgres + Redis +
NATS + Mongo, with `local-infra-creds` reflected into the `devai` namespace):

```sh
sandboxctl deploy --chart ../tesserix-k8s/charts/apps/local-infra \
  --name local-infra --no-build
```

> DevAI is also an AI control-plane play, so if you want the in-cluster agent
> gateway/registry add-ons, `sandboxctl up` supports `--with-agentgateway` /
> `--with-agentregistry` (optional; not required for DevAI itself).

## 3. Credentials & status

```sh
sandboxctl creds
sandboxctl status
```

## 4. Deploy DevAI

From the **devai repo root** (`cd /path/to/devai`):

```sh
# Datastores already exist (shared local-infra from step 2). DevAI connects to
# them via the reflected local-infra-creds Secret + stable DNS — nothing to
# deploy per-product. Just deploy the core services.

# Core services (API + SRE build from this repo; dashboards from their dirs)
sandboxctl deploy --repo .              --chart ../tesserix-k8s/charts/apps/devai-api            --name devai-api            --purge-old-tags
sandboxctl deploy --repo .              --chart ../tesserix-k8s/charts/apps/devai-sre            --name devai-sre            --purge-old-tags
sandboxctl deploy --repo dashboard      --chart ../tesserix-k8s/charts/apps/devai-dashboard      --name devai-dashboard      --purge-old-tags
sandboxctl deploy --repo sre-dashboard  --chart ../tesserix-k8s/charts/apps/devai-sre-dashboard  --name devai-sre-dashboard  --purge-old-tags
sandboxctl deploy --repo services/auth-bff --chart ../tesserix-k8s/charts/apps/devai-auth-bff    --name devai-auth-bff       --purge-old-tags
```

URLs: `https://devai-dashboard.sandbox.app:8443`,
`https://devai-sre-dashboard.sandbox.app:8443`, `https://devai-api.sandbox.app:8443`.

> A root `sandboxctl.yaml` multi-image manifest would collapse 4b into one
> `sandboxctl deploy` — ask and I'll generate it.

## 5. Point DevAI at the Agentic Registry (optional)

DevAI consumes a registry via its adapter family. To use the local
agentic-registry, set on the `devai-api` deploy (or its `values-local.yaml`):

```
DEVAI_REGISTRY_PROVIDER=tesserix
DEVAI_REGISTRY_URL=http://agentic-registry.agentic-registry.svc.cluster.local:8080
```

## 6. Keep images & disk clean

```sh
podman system prune -a -f --volumes && podman builder prune -af
```

## 7. Reset DevAI's data (shared local-infra)

DevAI shares the `local-infra` datastores, so reset only DevAI's slice (its
Postgres DB `devai_db`, Redis logical DB 0, and `devai` NATS streams) without
touching other products:

```sh
../tesserix-k8s/charts/apps/local-infra/clean-product.sh devai
```

## 8. Redeploy / tear down

```sh
sandboxctl deploy --repo . --chart ../tesserix-k8s/charts/apps/devai-api --name devai-api --purge-old-tags
sandboxctl undeploy --name devai-api
sandboxctl down
```

## Known pre-existing chart issues (flagged, not introduced)

- `devai-api/templates/runtime-rbac.yaml` references `.Values.k8sRuntime.enabled`,
  which is missing from base `values.yaml` (only in `values-prod.yaml`). The
  `values-local.yaml` overlay sets `k8sRuntime.enabled: false` so it renders.
- `devai-auth-bff/values.yaml` carries CPU requests/limits (against the
  memory-only policy). Left untouched here; fix separately.
