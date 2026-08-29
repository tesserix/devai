# Agent Substrate — production runbook

> **Status (2026-08-20): NO-GO, user traffic disabled.** Substrate 0.0.8,
> its CRDs, and the shared `kagent-default` WorkerPool are deployed through
> Argo CD. The keyless `devai-substrate-canary` is accepted by kagent, but is
> not ready because its golden ActorTemplate is assigned to a worker record for
> a deleted pod. The operator switch `DEVAI_KAGENT_ENABLED` must remain false
> until the canary is healthy and the isolation requirements in #76 are proven.

Context: tracking issues [#69](https://github.com/tesserix/devai/issues/69),
[#70](https://github.com/tesserix/devai/issues/70),
[#71](https://github.com/tesserix/devai/issues/71), and
[#76](https://github.com/tesserix/devai/issues/76).

The security boundary, attacker model, invariants, and mandatory cross-tenant
negative tests are defined in
[SUBSTRATE-THREAT-MODEL.md](SUBSTRATE-THREAT-MODEL.md).

## Current architecture

| Component | Current production state |
|---|---|
| Substrate charts | CRDs and runtime pinned to 0.0.8 |
| Runtime namespace | `ate-system` |
| kagent namespace | `kagent-system` |
| WorkerPool | `kagent-default`, one ready worker |
| Worker image | Digest-pinned `ateom-gvisor:v0.0.8` |
| Sandbox selection | `WorkerPool.spec.sandboxClass: gvisor` |
| Canary | `Accepted=True`, `Ready=False`, `ActorTemplateNotReady` |
| User traffic | Disabled by `DEVAI_KAGENT_ENABLED=false` |

The WorkerPool pod is a privileged `ateom-gvisor` container. It runs on a
regular GKE node and creates nested gVisor Actor sandboxes inside that worker.
The WorkerPool pod itself does **not** set `runtimeClassName: gvisor` and does
not schedule onto the `sandbox-gvisor` GKE node pool.

This distinction matters for both operations and the threat model. The nested
gVisor boundary isolates Actors from one another, while the privileged worker
remains a larger node-level blast radius. Do not describe the current topology
as “one GKE Sandbox pod per Actor” or enable untrusted user code before #76
proves the required tenant and workload-identity controls.

## GO/NO-GO decision for the three-node budget

The current decision for [#70](https://github.com/tesserix/devai/issues/70) is
**NO-GO**. This is a measured safety decision, not a claim that the Actor model
cannot work after its runtime and isolation dependencies mature.

| Gate | Production evidence | Result |
|---|---|---|
| Control-plane readiness | Canary `Accepted=True`, `Ready=False`, `ActorTemplateNotReady` | Fail |
| On-demand execution | No Actor can complete the keyless canary path | Fail |
| Fixed worker budget | Worker is Kubernetes `BestEffort`: no CPU or memory request/limit | Fail |
| Worker privilege | Substrate 0.0.8 generates a root, privileged worker with a hostPath | Fail |
| 5/20/50 Actor load | Cannot be run while the golden Actor is not ready | Not measurable |
| Cold-start latency | No successful idle-to-response sample exists | Not measurable |
| Dedicated pool | `sandbox-gvisor` exists with zero nodes and autoscaling disabled | Not usable |
| Three-node isolation | The worker currently shares an ordinary support node | Fail |

The existing worker does not consume one pod per Actor, but that fact alone is
not a capacity proof. Its missing resource request lets the scheduler treat it
as free, and its missing memory limit gives it no enforceable ceiling. The
current deployment also includes the Substrate control plane, six Valkey pods,
RustFS, and one worker, so a per-Actor cost cannot be separated from a reliable
fixed baseline yet.

Substrate 0.0.15 removes privileged mode from the gVisor worker, adds explicit
capabilities and authenticated tunnel identities, supports WorkerPool resource
templates, and replaces Valkey with PostgreSQL. It is not currently a safe
production upgrade: kagent 0.9.12 compiles against Substrate 0.0.6, the newest
published kagent 0.10.0-rc3 compiles against 0.0.9, and only unreleased kagent
`main` targets Substrate 0.0.15. The 0.0.15 chart also requires new signing
pools, an authentication ConfigMap, pod-certificate projections, and a stateful
Valkey-to-PostgreSQL replacement.

Re-open the GO decision only when all of these are true:

1. A published kagent release explicitly targets the selected hardened
   Substrate release.
2. The migration, signing-state bootstrap, and rollback are proven outside
   production.
3. The WorkerPool has enforceable CPU and memory requests/limits and the #76
   network boundary is default-deny.
4. The keyless canary is Ready and completes a wake-and-return invocation.
5. The 5/20/50 Actor tests record worker CPU, memory, pod count, failures, and
   p50/p95 cold-start latency without displacing a core DevAI workload.

Until then, ephemeral Jobs remain the supported on-demand runtime and preserve
the three-node budget with zero idle agent footprint.

## GitOps ownership

The production sources of truth are in `tesserix/tesserix-k8s`:

- `argocd/prod/infrastructure/substrate-crds.yaml`
- `argocd/prod/infrastructure/substrate.yaml`
- `argocd/prod/infrastructure/kagent.yaml`
- `charts/apps/kagent-agent-sync/`

The old staged manifests under `argocd/prod/apps/substrate/` are superseded.
Do not recreate or sync a second Argo CD Application for the same Helm release.

Argo CD sync ordering is:

1. Substrate CRDs at sync wave `-6`.
2. Substrate runtime at sync wave `-5`.
3. kagent and its WorkerPool at sync wave `-3`.
4. Registry-to-kagent reconciliation through `kagent-agent-sync`.

All changes must follow that GitOps path. Do not use `kubectl apply` to repair
or deploy production resources.

## OIDC and protected signing state

Substrate 0.0.8 is required because it trusts system roots for an external OIDC
issuer. The Helm values set the GKE issuer explicitly:

```text
https://container.googleapis.com/v1/projects/tesseracthub-480811/locations/asia-south1/clusters/tesseract-prod-in-gke
```

This resolved the earlier x509 and unexpected-issuer failures between kagent
and `ate-api`.

Argo CD deliberately preserves the generated TLS and Actor/session signing
material across chart upgrades:

- `ConfigMap/ateapi-ca`
- `Secret/ateapi-tls`
- `Secret/session-id-ca-pool`
- `Secret/session-id-jwt-pool`

Their production creation timestamp is `2026-06-17T14:37:57Z`. Verify it is
unchanged before and after any Substrate upgrade or state repair. Never print
their data.

## Registry reconciliation and canary

`kagent-agent-sync` exports registry agents with
`workerPool=kagent-default`, then reconciles and prunes both
`SandboxAgent` and legacy `Agent` resources. The reconciler:

- selects registry agents labelled `devai.io/runtime=kagent`;
- resolves `promptRef` before rendering a non-empty `systemMessage`;
- emits `kagent.dev/v1alpha2` `SandboxAgent` resources;
- applies the configured model variants; and
- removes resources after a successful empty export.

The GitOps-managed `devai-substrate-canary` has no provider key, model
invocation, tool call, or user payload. It only verifies the
`SandboxAgent → ActorTemplate → Actor` control-plane path.

Current blocker:

```text
Accepted=True  reason=Reconciled
Ready=False    reason=ActorTemplateNotReady
               message=ActorTemplate golden snapshot is not ready
```

The golden Actor is assigned to a deleted WorkerPool pod. Substrate 0.0.8 can
miss worker deletion events while its API is unavailable and does not reconcile
orphan workers on startup.

## Agentic Gateway routing

Pull request
[tesserix-k8s#432](https://github.com/tesserix/tesserix-k8s/pull/432)
routes kagent's Anthropic and OpenAI ModelConfigs through the private AI gateway:

```text
Anthropic: http://ai-gateway.agentgateway-system.svc.cluster.local:8080/anthropic
OpenAI:    http://ai-gateway.agentgateway-system.svc.cluster.local:8080/openai/v1
```

The ModelConfigs retain `apiKeyPassthrough`; no provider key is stored in the
canary. The user-specific key is forwarded only for the selected request.

This is necessary but not sufficient for tenant-safe user traffic:

- gateway authorization must bind Actor calls to the correct workload identity;
- every request needs server-derived tenant, user, and run attribution for cost;
- cross-user Actor memory and state isolation needs a negative test; and
- provider egress must fail closed instead of allowing a direct-provider bypass.

Do not enable `DEVAI_KAGENT_ENABLED` or broaden the gateway allowlist until
those #76 acceptance checks pass.

`DEVAI_AGENTGATEWAY_URL` is the MCP gateway base used by DevAI Jobs and
runners. It is separate from the AI provider base URL above. Provider adapters
must use the configured AI gateway paths; MCP endpoint resolution must use
`DEVAI_AGENTGATEWAY_URL`. Neither variable is an authorization mechanism by
itself.

## Valkey and stale-worker incident

The six Valkey pods are ready and cover all 16,384 slots, but cluster membership
contains stale pod addresses. At least one master advertises an old address and
one failed, addressless node remains. Clients can therefore be redirected to a
dead address.

The WorkerPool store also contains non-expiring records for deleted worker
pods. The affected records inspected on 2026-08-19 held no Actor assignment or
user data, but deleting them is still a production state repair.

Before any repair:

1. Obtain explicit approval for the named records and cluster nodes.
2. Capture Valkey membership, the stale records, and canary state in a
   restricted recovery directory.
3. Verify every target against the current Kubernetes pod set.
4. Repair only the stale membership and orphan worker records.
5. Recreate only the keyless canary through GitOps if it cannot recover.
6. Re-run every verification below and confirm the protected signing-state
   timestamps did not change.

Do not paste Valkey payloads into an issue, pull request, log, or this runbook.

Later Substrate releases add startup orphan-worker reconciliation and replace
Valkey with PostgreSQL. Treat that as a separate, reviewed migration; do not
upgrade solely as an incident workaround.

## Read-only verification

Always use the production kubeconfig explicitly:

```bash
export KUBECONFIG=/Users/samyakrout/.kube/gke-prod

gcloud config get-value account
gcloud config get-value project
kubectl config current-context

kubectl -n ate-system get pods
kubectl -n kagent-system get workerpool kagent-default
kubectl -n kagent-system get sandboxagent devai-substrate-canary
kubectl -n kagent-system describe sandboxagent devai-substrate-canary
kubectl -n kagent-system get actortemplate
kubectl -n kagent-system get modelconfig
```

Expected identity and target:

```text
account:   unidevidp@gmail.com
project:   tesseracthub-480811
context:   gke_tesseracthub-480811_asia-south1_tesseract-prod-in-gke
namespace: kagent-system
```

Inspect the WorkerPool execution boundary without printing secret data:

```bash
kubectl -n kagent-system get workerpool kagent-default \
  -o jsonpath='{.spec.sandboxClass}{"\n"}{.spec.ateomImage}{"\n"}'

kubectl -n kagent-system get pods \
  -l ate.dev/worker-pool=kagent-default \
  -o jsonpath='{range .items[*]}{.metadata.name}{" runtimeClass="}{.spec.runtimeClassName}{" node="}{.spec.nodeName}{"\n"}{end}'
```

The first command should report `gvisor`; an empty `runtimeClass` on the
WorkerPool pod is expected for the current nested-gVisor architecture.

Verify status conditions and gateway configuration without credentials:

```bash
kubectl -n kagent-system get sandboxagent devai-substrate-canary \
  -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{" reason="}{.reason}{"\n"}{end}'

kubectl -n kagent-system get modelconfig \
  -o jsonpath='{range .items[*]}{.metadata.name}{" provider="}{.spec.provider}{"\n"}{end}'
```

## Completion gate for #71

Do not close #71 until all of the following are observed:

- the keyless canary is `Accepted=True` and `Ready=True`;
- its ActorTemplate golden snapshot is ready;
- its Actor is assigned to the single live WorkerPool pod;
- no new x509, OIDC issuer, dead-address, or Valkey timeout error appears;
- all protected signing resources retain creation timestamp
  `2026-06-17T14:37:57Z`;
- Argo CD reports the Substrate, kagent, and agent-sync applications synced and
  healthy; and
- this runbook is deployed from DevAI `main`.

## Rollback and upgrade policy

Rollback is a Git revert of the owning `tesserix-k8s` commit followed by Argo
CD reconciliation. Do not delete the live Argo CD Applications, CRDs, Valkey
StatefulSet, or protected signing resources as a rollback shortcut.

Before an upgrade:

1. Diff CRDs, rendered names, StatefulSets, PVCs, signing resources, and Valkey
   versions between the current and candidate charts.
2. Prove the migration and rollback in a non-production cluster.
3. Back up or capture every stateful resource needed to return to 0.0.8.
4. Confirm Argo CD ignores generated signing data under the candidate names.
5. Roll out through GitOps and verify the canary before enabling user traffic.

## Cost and capacity

The current one-worker pool is intended to multiplex Actors and does not create
one Kubernetes pod or GKE node per Actor. The WorkerPool currently has no
resource request or limit, however, and the canary cannot reach Ready. Measure
5, 20, and 50 concurrent Actors before changing the NO-GO decision or setting
production quotas. Until those measurements exist, no supported concurrency or
cost-per-Actor claim should be made.

The dedicated `sandbox-gvisor` GKE node pool still exists with zero nodes and
autoscaling disabled, but the current WorkerPool does not schedule onto it.
Enabling, resizing, removing, or repurposing it is a separate production change
and requires explicit approval.
