# DevAI Deploy & Verify Runbook — Agentic Control Planes

This runbook covers the **end-to-end deployment** of DevAI on the prod GKE
cluster (`gke_tesseracthub-480811_asia-south1_tesseract-prod-in-gke`),
including the three new Solo.io control planes (aregistry, agentgateway,
kagent) plus the existing DevAI workload (api, sre, dashboards, postgres,
auth-bff) plus the registry bootstrap.

Per CLAUDE.md, **all changes go through ArgoCD via `tesserix-k8s`** —
this runbook never uses `kubectl apply`. The actions below are git
push → ArgoCD sync → verify.

---

## TL;DR — order of operations

```
1. Provision the aregistry-jwt secret  (ExternalSecret + GCP SM)
2. git push tesserix-k8s/main          (commits all the new ArgoCD apps)
3. argocd app sync infrastructure-app-of-apps    (wave -5/-4/-3 control planes)
4. argocd app sync ai-apps-app-of-apps           (devai workload + bootstrap)
5. Verify each layer (commands at the bottom of this doc)
```

---

## Layer 1 — Agentic Control Planes (Solo.io OSS)

| Layer | Namespace | Sync wave | Chart |
|---|---|---|---|
| **aregistry** | `agentregistry-system` | `-5` | `charts/thirdparty/agentregistry` |
| **agentgateway** | `agentgateway-system` | `-4` | `charts/thirdparty/agentgateway` |
| **kagent** | `kagent-system` | `-3` | `charts/thirdparty/kagent` |

ArgoCD app definitions are in `tesserix-k8s/argocd/prod/infrastructure/`
and are enrolled in the kustomization at the bottom of that file's
`resources:` list.

### Required secret: `aregistry-jwt`

All three control planes need the same JWT-signing secret. Create it
in GCP Secret Manager once:

```bash
echo -n "$(openssl rand -base64 64 | tr -d '\n')" | \
  gcloud secrets create prod-aregistry-jwt-secret \
    --project=tesseracthub-480811 \
    --replication-policy=automatic \
    --data-file=-
```

Then add an `ExternalSecret` in `tesserix-k8s/external-secrets/prod/agentregistry-system/`
that pulls it to a `Secret` named `aregistry-jwt` with key `jwt-secret`.
Mirror it to `agentgateway-system` and `kagent-system` via Reflector
annotations.

### Verify

```bash
kubectl get pods -n agentregistry-system    # aregistry-* Running, aregistry-pg-* Running
kubectl get pods -n agentgateway-system     # agentgateway-* Running
kubectl get pods -n kagent-system           # kagent-controller-* Running

# Quick health probes
kubectl exec -n agentregistry-system deploy/agentregistry -- curl -s http://localhost:8080/healthz
kubectl exec -n agentgateway-system deploy/agentgateway -- curl -s http://localhost:8080/healthz
```

### Verify upstream image references

The wrapper charts use **placeholder** image refs. Before the first sync,
update `values.yaml` in each chart with the verified upstream tag:

- aregistry: https://github.com/solo-io/agentregistry/pkgs/container/agentregistry
- agentgateway: https://github.com/agentgateway/agentgateway/pkgs/container/agentgateway
- kagent: https://github.com/kagent-dev/kagent/pkgs/container/kagent

The placeholder URLs in `values.yaml` are commented; replace with the
verified ones for your release line.

---

## Layer 2 — DevAI Workload

| App | Namespace | Chart |
|---|---|---|
| `devai-postgres` | `devai` | CNPG cluster for DevAI's main DB |
| `devai-auth-bff` | `devai` | OIDC-bridge for the dashboards |
| `devai-db-seed` | `devai` | one-shot pgvector + schema bootstrap |
| `devai-istio` | `devai` | VirtualServices + AuthorizationPolicies |
| `devai-api` | `devai` | the FastAPI ALM service (port 8080) |
| `devai-sre` | `devai` | the SRE FastAPI service (port 8090) |
| `devai-dashboard` | `devai` | ALM Next.js dashboard (port 3100) |
| `devai-sre-dashboard` | `devai` | SRE Next.js dashboard (port 3200) |

All eight charts already exist in `tesserix-k8s/charts/apps/devai-*`.
They were previously **not** enrolled in `argocd/prod/apps/ai-apps/kustomization.yaml`
— this PR enrolls them.

### Feature flags now in `values-prod.yaml`

The DevAI image picks up the Fiber-style runtime when these are set:

| Env var | Default in chart | Notes |
|---|---|---|
| `DEVAI_PIPELINE_ENABLED` | `true` | master switch for the blueprint runtime |
| `DEVAI_PIPELINE_DEFAULT_BLUEPRINT` | `alm-pipeline` | what `POST /api/pipeline/runs` defaults to |
| `DEVAI_PIPELINE_SRE_BLUEPRINT` | `sre-monitor` | sre server uses this |
| `DEVAI_SPECIALIZATIONS_ENABLED` | `true` | mount the YAML role catalog |
| `DEVAI_MEMORY_PROVIDER` | `pgvector` | adapter family: noop \| redis \| pgvector \| mem0 \| zep \| hondo |
| `DEVAI_AREGISTRY_URL` | `http://agentregistry.agentregistry-system.svc.cluster.local:8080` | |
| `DEVAI_AGENTGATEWAY_URL` | `http://ai-gateway.agentgateway-system.svc.cluster.local` | |

Flip `DEVAI_PIPELINE_ENABLED=false` to revert to the legacy LangGraph
orchestrator without redeploying.

### Verify

```bash
kubectl get pods -n devai
# expected: devai-postgres-1, devai-auth-bff-*, devai-api-*, devai-sre-*,
#          devai-dashboard-*, devai-sre-dashboard-*

# REST surface
kubectl port-forward -n devai svc/devai-api 8080:80 &
curl -s http://localhost:8080/healthz | jq
curl -s http://localhost:8080/api/pipeline/blueprints | jq 'length'   # → 4
curl -s http://localhost:8080/api/pipeline/stages | jq 'length'        # → 33
curl -s http://localhost:8080/api/specializations | jq 'length'        # → 26
```

---

## Layer 3 — Registry Bootstrap

After aregistry is healthy, the `devai-registry-bootstrap` ArgoCD app
(`tesserix-k8s/argocd/prod/apps/ai-apps/devai-registry-bootstrap.yaml`)
fires a one-shot Job that POSTs every CR under
`architecture/registry-seeds/` to aregistry's v0 API.

| Resource | Count | Generated from |
|---|---|---|
| Project | 1 | hand-written |
| MCPServer | 4 | hand-written (devai-mcp, sre-mcp, scm-mcp, analyst-mcp) |
| Skill | 26 | `scripts/generate_registry_seeds.py` from `specializations/*.yaml` |
| Agent | 26 | same generator |
| Prompt | 26 | same generator |
| **Total** | **83** | |

Re-syncs are safe — aregistry upserts on `(kind, name, namespace)`.

### Regenerating seeds when specs change

```bash
make registry-seeds          # rewrites architecture/registry-seeds/{skills,agents,prompts}/
make registry-seeds-check    # CI guard — exits 1 if generator output differs from disk
git commit -am "chore(registry): regenerate seeds"
git push
# ArgoCD re-syncs devai-registry-bootstrap → new Job picks up the new seeds
```

### Verify

```bash
# Job ran successfully:
kubectl get jobs -n devai -l app.kubernetes.io/name=devai-registry-bootstrap
kubectl logs -n devai -l app.kubernetes.io/name=devai-registry-bootstrap --tail=50

# Catalog populated:
kubectl exec -n agentregistry-system deploy/agentregistry -- curl -s \
  http://localhost:8080/v0/skills | jq 'length'    # → 26
```

---

## End-to-end smoke test (production)

Once everything is up:

```bash
# 1. List blueprints
curl -s https://devai.tesserix.app/api/pipeline/blueprints | jq '.[].name'

# 2. List specializations
curl -s https://devai.tesserix.app/api/specializations | jq 'group_by(.category) | map({key: .[0].category, count: length})'

# 3. Dispatch a security-scan blueprint
curl -s -X POST https://devai.tesserix.app/api/pipeline/runs \
  -H "Content-Type: application/json" \
  -d '{"intent": "production smoke test", "blueprint": "security-scan", "repo": "tesserix/devai-smoke"}' \
  | jq

# 4. Stream stage events
curl -N https://devai.tesserix.app/api/pipeline/events/stream
# expect SSE frames: event: stage / data: {...}

# 5. Check aregistry health
curl -s https://devai.tesserix.app/api/pipeline/stages | jq 'length'
```

---

## Rollback

Every layer is reversible without code changes.

| To revert | Action |
|---|---|
| Disable blueprint runtime | `kubectl set env -n devai deploy/devai-api DEVAI_PIPELINE_ENABLED=false` — **NO** — use ArgoCD: edit `values-prod.yaml`, commit, push, sync. |
| Remove agentic control planes | Revert the kustomization commit in `infrastructure/` — ArgoCD prunes the apps. Workload keeps running because the URLs gracefully degrade. |
| Drop a specific control plane | Comment its line out of `infrastructure/kustomization.yaml`, push. |
| Wipe the registry catalog | `kubectl delete -n devai job -l app.kubernetes.io/name=devai-registry-bootstrap`, then `kubectl delete clusters -n agentregistry-system aregistry-pg` (this DESTROYS data). Re-sync to recreate. |

---

## Open questions / follow-ups

These are intentionally out of scope for this initial deploy but should
be filed as follow-up work:

1. **Upstream image refs** — the wrapper charts use placeholder
   `quay.io/solo-io/agentregistry:v0.3.3` etc. Verify against the actual
   Solo.io release channel before sync.
2. **mTLS between control planes** — Istio sidecars are enabled (`istio-injection=enabled`)
   on each namespace, but the `AuthorizationPolicy` for the new namespaces
   isn't yet in `istio-auth-policies.yaml`. Without it, mesh-wide
   `deny-all-default` may block traffic. See CLAUDE.md §5 in the parent
   workspace for the three-step Istio routing pattern.
3. **HPA on aregistry / agentgateway** — both are single-replica today.
   Scale up via KEDA once we have traffic curves.
4. **Backup / restore for aregistry's CNPG cluster** — needs to plug
   into the global CNPG backup pattern.
5. **Per-role TrafficPolicy** — the `policies: []` block in agentgateway
   values.yaml is intentionally empty. Lock down per-role tool access at
   the gateway level once we have telemetry showing which roles actually
   need which MCP tools.
