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
