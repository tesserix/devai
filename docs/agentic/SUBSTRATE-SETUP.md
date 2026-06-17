# Agent Substrate — prod setup runbook

> **Status (2026-06-17): staged, NOT yet deployed.** Blocked on step 1 (a GKE-Sandbox
> node pool). Everything below is verified against the live prod CRDs (a rendered
> SandboxAgent passes server-side dry-run); the manifests in
> `tesserix-k8s/argocd/prod/apps/substrate/` are ready but intentionally **not wired
> into an auto-syncing app-of-apps**, so committing them can't deploy a broken
> Substrate. Do the steps **in order**; do not enable substrate on the kagent
> controller until the sandbox pool + substrate controller are up (step 4 last).

Context: see `KAGENT-INTEGRATION.md` §0 (why kagent is dormant) and tracking issues
**#70** (GO/NO-GO), **#71** (install), epic **#69**.

## Why this is needed

The Agent Substrate runs each agent as a gVisor-sandboxed **Actor** in a shared
**WorkerPool** — low overhead, fast cold start, strong isolation (the model that
makes kagent fit our budget and lets *users* run *their own* agents safely). The
WorkerPool's worker (`ateom-gvisor`) runs under `runtimeClassName: gvisor`, which
**only schedules on GKE-Sandbox nodes**. Our 3 `optimized-v2` nodes are NOT
sandbox-enabled, so a WorkerPool pod would sit `Pending` forever. → we need a
dedicated, **autoscale-to-zero** sandbox node pool.

This does **not** need a separate cluster — a separate **node pool** in
`tesseract-prod-in-gke` is enough. A separate cluster is a later hardening option
(full tenant blast-radius isolation), not a requirement.

## Cluster facts (measured)

| | |
|---|---|
| Cluster | `tesseract-prod-in-gke`, region `asia-south1` (zones a/b/c) |
| Existing pool | `optimized-v2`, 3× `e2-standard-8` |
| gVisor RuntimeClass | present (`gvisor`) |
| SandboxAgent CRD | present (kagent-crds 0.9.7); WorkerPool CRD + substrate controller absent |
| Project | `tesseracthub-480811` |

---

## Step 1 — Create the GKE-Sandbox node pool (operator runs; cluster change, not GitOps)

A new, **dedicated** pool (gVisor can't share a node with normal workloads),
pinned to **one zone**, **autoscale 0→1** so it costs nothing when idle and a
single node only while agents actually run.

```bash
gcloud container node-pools create sandbox-gvisor \
  --cluster=tesseract-prod-in-gke \
  --region=asia-south1 \
  --node-locations=asia-south1-a \
  --sandbox type=gvisor \
  --machine-type=e2-standard-4 \
  --num-nodes=0 \
  --enable-autoscaling --min-nodes=0 --max-nodes=1 \
  --node-labels=workload=substrate \
  --shielded-secure-boot --shielded-integrity-monitoring \
  --project=tesseracthub-480811
```

Notes:
- `--sandbox type=gvisor` makes it a GKE-Sandbox pool; GKE auto-adds the taint
  `sandbox.gke.io/runtime=gvisor:NoSchedule` and the runtime label, so only
  gVisor (`runtimeClassName: gvisor`) pods land here — nothing else is disturbed.
- `e2-standard-4` (4 vCPU / 16 GB) is plenty: the WorkerPool **multiplexes** many
  Actors onto one node (it is NOT a node-per-agent). Resize later if needed.
- `min-nodes=0` = **scale-to-zero** → baseline stays exactly 3 nodes; the sandbox
  node appears on demand and drains when idle. This honors the 3-node constraint.
- Single zone keeps it to **one** sandbox node max. Go regional later for HA.

Verify:
```bash
kubectl get nodes -l sandbox.gke.io/runtime=gvisor   # 0 when idle (scaled to zero) — that's expected
gcloud container node-pools describe sandbox-gvisor --cluster=tesseract-prod-in-gke --region=asia-south1
```

## Step 2 — Pin the substrate chart versions (before any prod sync)

The substrate charts are young; do **not** float `latest` on prod. Resolve the
real versions and set them in the app YAMLs:
```bash
helm show chart oci://ghcr.io/kagent-dev/substrate/helm/substrate-crds | grep ^version
helm show chart oci://ghcr.io/kagent-dev/substrate/helm/substrate      | grep ^version
```
Put those in `argocd/prod/apps/substrate/substrate-crds.yaml` and `substrate.yaml`
(`spec.source.targetRevision`).

## Step 3 — Install Substrate via ArgoCD (CRDs → controller)

Wire the staged apps into the app-of-apps and sync **in order**:
```bash
# add to tesserix-k8s/argocd/prod/apps/ai-apps/kustomization.yaml:
#   - ../substrate/substrate-crds.yaml
#   - ../substrate/substrate.yaml
# commit + push; then:
argocd app sync substrate-crds && argocd app wait substrate-crds --health
argocd app sync substrate      && argocd app wait substrate      --health
kubectl get crd | grep -i workerpool         # WorkerPool CRD now present
kubectl -n ate-system get pods               # substrate controller Running
```
The substrate **controller** is a normal pod (no gVisor) — it runs on the existing
pool. Only the WorkerPool's worker needs the sandbox node (step 4).

## Step 4 — Enable substrate on kagent + create the WorkerPool (LAST)

Only after step 1 + 3 succeed. Add to the `kagent` ArgoCD app's helm values
(`tesserix-k8s/argocd/prod/infrastructure/kagent.yaml`) — this reconfigures the
running controller, so it goes last:
```yaml
        controller:
          substrate:
            enabled: true
            defaultWorkerPool:
              namespace: kagent-system
              name: kagent-default
            ateApiEndpoint: "dns:///api.ate-system.svc:443"
            ateApiInsecure: true
            atenetRouterURL: "http://atenet-router.ate-system.svc:80"
            ateApiTokenFile: "/var/run/secrets/tokens/ate-api/token"
        substrateWorkerPool:
          create: true
          name: kagent-default
          replicas: 1
          ateomImage: ghcr.io/kagent-dev/substrate/ateom-gvisor:v0.0.6
```
Verify the WorkerPool schedules (it triggers the sandbox node to autoscale up):
```bash
kubectl -n kagent-system get workerpool kagent-default
kubectl -n kagent-system get pods -l app=substrate-worker -o wide   # lands on the gvisor node
kubectl get nodes -l sandbox.gke.io/runtime=gvisor                  # 1 node now (autoscaled up)
```

## Step 5 — End-to-end smoke test (DevAI → SandboxAgent → Actor)

```bash
# render a SandboxAgent from a DevAI registry agent (workerPool param):
curl -s "http://agentregistry.agentregistry-system:12121/v0/agents/document-analyzer-agent/export/kagent?namespace=devai&workerPool=kagent-default&modelConfig=default-model-config"
# cross-validate first (ok=true means the controller will accept it):
curl -s ".../export/kagent?...&validate=true"   # {"ok":true,"issues":[]}
# apply via the kagent-agent-sync path (NOT manual kubectl) — see below — then:
kubectl -n kagent-system get sandboxagent
```
Already proven: a rendered SandboxAgent **passes the live CRD** (server dry-run).

## Wiring DevAI → Substrate (after the runtime is up)

- `kagent-agent-sync` (CronJob) renders + applies the labelled agents. To target
  Substrate, pass `?workerPool=kagent-default` to the export (agentic-registry
  already supports it → emits `SandboxAgent` with `spec.substrate.workerPoolRef`).
- Re-enable kagent platform-wide: `DEVAI_KAGENT_ENABLED=true` (devai-api values) —
  this is the operator kill-switch; per-user enablement still applies.
- Dispatch (`_maybe_dispatch_kagent`) reaches the Actor over A2A unchanged.

## Rollback

- Step 4: revert the kagent values change → ArgoCD restores the controller; the
  WorkerPool is pruned.
- Step 3: `argocd app delete substrate substrate-crds`.
- Step 1: `gcloud container node-pools delete sandbox-gvisor …` (scaled to zero, so
  deleting when idle is free).

## Cost / footprint

- Idle: **0** extra nodes (scale-to-zero) — baseline unchanged at 3.
- Active: **1** `e2-standard-4` sandbox node while agents run; the WorkerPool
  multiplexes all Actors onto it. Substrate controller (~1 small pod) on the
  existing pool.

---

## Decision log (why it's shaped this way)

| Question | Decision | Why |
|---|---|---|
| Run agents always-warm (classic kagent) or on-demand? | **On-demand Jobs by default** | A classic kagent Agent = one standing Deployment per agent × model variant → ~160 pods at our scale → doesn't fit 3 nodes. The default `JobRunnerStage` spins an ephemeral Job per run; per-user keys + fallback already work there. |
| Is kagent dead, then? | **No — dormant, behind `DEVAI_KAGENT_ENABLED` (off)** | Kept for genuinely hot agents once there's headroom. The operator flag is the real off-switch (provisioning is operator-controlled; per-user toggles only pick *which* variants). |
| Why revisit kagent at all? | **Agent Substrate** | WorkerPool + Actor model = low overhead + fast cold start + gVisor isolation per agent → fits the budget *and* lets *users* run *their own* agent code safely. |
| Separate cluster for Substrate? | **No — a node pool** | Substrate needs gVisor *nodes*, i.e. a GKE-Sandbox node pool in the same cluster. Separate cluster = later hardening for full tenant blast-radius isolation, not a requirement. |
| How to keep 3-node baseline? | **Sandbox pool autoscale 0→1** | 0 nodes when idle (baseline = 3), 1 sandbox node on demand. The WorkerPool multiplexes Actors onto it — not a node-per-agent. |
| How do we know our render is correct before deploying? | **Server-side dry-run + a validate API** | `kubectl apply --dry-run=server` validates against the live CRD + admission with **no persist**; `GET /v0/agents/{n}/export/kagent?validate=true` returns `{ok,issues}` for authoring. |

## Gotchas & lessons learned (the hard-won ones)

**Schema / rendering (agentic-registry `adapters/kagent`)**
1. **systemMessage must be non-empty** or the controller rejects the CR. 39/40 DevAI
   agents keep their prompt in a referenced `Prompt` (not inline) → the export must
   resolve `spec.promptRef` → `Prompt.spec.systemPrompt` (`resolveSystemPrompt`).
2. **`spec.substrate.workerPoolRef` is an OBJECT `{name[,apiGroup,kind]}`, not a
   string.** A string is rejected (`must be of type object`). Caught by server dry-run.
3. **Nest under `spec.declarative` + `spec.type: Declarative`.** A flat/pre-0.9 spec
   makes the controller nil-panic (declarative pruned to nil).
4. **Emit `kagent.dev/v1alpha2`.** The declarative shape only exists there; v1alpha1
   converts-and-drops the model/prompt on storage.
5. **Separate multi-doc YAML with `---`.** Concatenated docs parse as ONE (last wins),
   so only the final agent/variant applies.
6. **Model ids must be DIRECT-provider-valid.** kagent calls the provider directly
   (no DevAI gateway), so a gateway-alias model 404s; validate 200/429 vs 404.

**Apply / reconcile (`kagent-agent-sync`)**
7. **Client-side apply, not `--server-side`.** SSA mis-negotiates the multi-version
   CRD and rejects `spec.type`/`spec.declarative` ("field not declared in schema").
8. **`--validate=false` + 256Mi.** Client-side apply downloads/parses the cluster
   OpenAPI → OOMKill (exit 137) at 128Mi.
9. **The registry `/v0/apply` MERGES `metadata.labels`.** Removing a label from a seed
   + reseeding does NOT drop it on the registry object → the agent still exports. The
   reliable off-switch is the **active-variants kill-switch** (operator `kagent_enabled`),
   not unlabelling.
10. **Reap on a *successful empty* export, not keep-last.** Otherwise unlabelling the
    last agent orphans its pods forever.

**Cluster / mesh / runtime**
11. **gVisor needs a GKE-Sandbox node pool.** The `gvisor` RuntimeClass *existing* is
    NOT enough — a pod with `runtimeClassName: gvisor` only schedules on a
    `--sandbox type=gvisor` node pool. No such pool → Pending forever.
12. **Cross-namespace on ambient mesh = THREE layers.** kagent-system → devai-api needed
    a NetworkPolicy **ingress** + an **egress** allow + an **Istio AuthorizationPolicy**
    SPIFFE principal — ztunnel L4-resets by identity *before* the L7 token check, so the
    authz was the real unlock. A NetworkPolicy alone isn't enough.
13. **Substrate namespace is `ate-system`; worker image is `ateom-gvisor`.** ("ate" =
    the substrate runtime; not "substrate-system".)

**Process / git**
14. **`connect-local` / `connect-prod` first; verify `kubectl config current-context`.**
    The default context may be prod. Read-only inspection on prod is fine; deploys go
    through ArgoCD (never manual `kubectl apply`).
15. **A pre-existing unpushed local commit can hide under `main`.** When pushing
    tesserix-k8s, a stray local commit (e.g. `d025be3f` HomeChef NATS, not ours) caused a
    rebase conflict. **Cherry-pick your own commit onto `origin/main`** and push that —
    don't rebase-and-lose someone else's WIP.
16. **`dashboard/next-env.d.ts` + `tsconfig.json` are Next.js auto-reformats.** They show
    as modified from session start; do NOT commit them with feature changes.

## Verify / reproduce cheatsheet

```bash
# 0. context (NEVER assume)
kubectl config current-context

# 1. Substrate readiness
kubectl get crd | grep -iE 'sandboxagent|workerpool'         # SandboxAgent yes, WorkerPool = installed?
kubectl get runtimeclass gvisor                              # RuntimeClass present?
kubectl get nodes -l sandbox.gke.io/runtime=gvisor           # a sandbox NODE? (empty = blocker)
kubectl -n ate-system get pods                               # substrate controller up?

# 2. cross-validate a render WITHOUT deploying (the ultimate check)
cat <<'Y' | kubectl apply --dry-run=server -f -
apiVersion: kagent.dev/v1alpha2
kind: SandboxAgent
metadata: {name: probe, namespace: kagent-system}
spec:
  type: Declarative
  platform: substrate
  substrate: {workerPoolRef: {name: kagent-default}}   # OBJECT, not string
  declarative: {runtime: go, modelConfig: default-model-config, systemMessage: "hi"}
Y
# "created (server dry run)" = passes the live CRD + admission, nothing persisted.

# 3. cross-validate from DevAI / the registry (returns {ok, issues})
curl -s ".../v0/agents/<name>/export/kagent?namespace=devai&workerPool=kagent-default&validate=true"
# or in Python: RegistryClient(...).kagent_validate("<name>")

# 4. render a SandboxAgent (no deploy)
curl -s ".../v0/agents/<name>/export/kagent?namespace=devai&workerPool=kagent-default&modelConfig=default-model-config"
```

## As actually deployed (2026-06-17) — the real steps + fixes hit

This is what *actually* happened bringing it up on prod, beyond the idealized
runbook above. Reproduce in this order.

1. **Node pool** — `gcloud container node-pools create sandbox-gvisor … --sandbox type=gvisor`
   (the command at the top). Created fine, scale-to-zero.
2. **Pin versions** — substrate charts: latest is **0.0.6** (`crane ls ghcr.io/kagent-dev/substrate/helm/substrate`).
   `helm show/pull` was blocked locally by a missing `docker-credential-osxkeychain`
   — use **`crane`** to inspect OCI charts instead.
3. **Wire the apps** — `prod-infrastructure` is **kustomize-based**, so the two
   Application files (`argocd/prod/infrastructure/substrate-{crds,}.yaml`) must be
   **added to `argocd/prod/infrastructure/kustomization.yaml` `resources:`** — a
   standalone file in the dir is otherwise invisible. (Cost me a wrong assumption +
   a wasted sync.)
4. **CRDs are `ate.dev`, not `kagent.dev`** — `workerpools.ate.dev`,
   `actortemplates.ate.dev`. The substrate stack lands in **`ate-system`**:
   `ate-api-server`, `ate-controller`, `atelet` (×3 worker daemons), `atenet-router`,
   `dns`, `rustfs` (object store), plus a bundled valkey (the `substrate` app shows a
   benign valkey StatefulSet `OutOfSync` — Healthy, ignore).
5. **Enable on kagent (Phase B)** — `controller.substrate.enabled=true` +
   `substrateWorkerPool.create=true` in the **kagent** app values. The new
   substrate-enabled controller pod **CrashLoopBackOff**ed:
   `dial ate-api "dns:///api.ate-system.svc:443": context deadline exceeded`.
   **Cause:** `kagent-system` egress is default-deny and `ate-system` wasn't allowed.
   **Fix:** `allow-kagent-to-ate-egress` NetworkPolicy in
   `manifests/agentic-istio/networkpolicy-consumer-egress.yaml` (same pattern as the
   registry/devai egress allows). Controller recovered, WorkerPool worker came up.
6. **SandboxAgent stuck `ActorTemplateNotReady`** — the ate-controller couldn't
   create the "golden actor": `Unauthenticated: invalid bearer token: unexpected
   issuer "https://container.googleapis.com/.../tesseract-prod-in-gke"`.
   **Cause:** **GKE mints SA tokens with the cluster's OIDC issuer**, but the ate-api
   defaulted `auth.jwt.issuer=https://kubernetes.default.svc.cluster.local`.
   **Fix:** set `auth.jwt.issuer` (substrate app values) to this cluster's issuer
   (`kubectl get --raw /.well-known/openid-configuration`).
7. **git** — `tesserix-k8s` main is force-pushed by other clones and **dropped a
   clean push** mid-deploy. Always `git pull --rebase origin main` then push; verify
   with `git ls-tree origin/main <path>`. (See the feedback memory.)

## Where everything lives

| Piece | Path |
|---|---|
| This runbook | `devai/docs/agentic/SUBSTRATE-SETUP.md` |
| Jobs-vs-kagent decision + re-enable | `devai/docs/agentic/KAGENT-INTEGRATION.md` §0 / §0a |
| Render + validate + SandboxAgent | `agentic-registry/adapters/kagent/kagent.go`, `internal/api/export.go`, `resolve.go` |
| DevAI validate client | `devai/src/devai/registry/client.py::kagent_validate` |
| Reconciler (renders + applies) | `tesserix-k8s/charts/apps/kagent-agent-sync/` |
| Staged Substrate ArgoCD apps | `tesserix-k8s/argocd/prod/apps/substrate/` (manual sync, not wired) |
| kagent controller app | `tesserix-k8s/argocd/prod/infrastructure/kagent.yaml` |
| Tracking | tesserix/devai epic **#69**, subs **#70–#78** |
