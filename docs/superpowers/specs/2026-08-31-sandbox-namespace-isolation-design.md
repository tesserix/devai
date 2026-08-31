# Sandbox namespace isolation — design

Date: 2026-08-31
Status: approved for planning

## Problem

Every sandbox today runs in the shared `devai` namespace, fenced only by
label-selected ResourceQuota/LimitRange/NetworkPolicy objects. Two consequences:

1. A label or policy mistake is cross-sandbox (and cross-user) exposure —
   isolation is policy-enforced, not boundary-enforced.
2. The egress NetworkPolicy must allow the whole `devai` namespace so the
   sandbox can reach the control plane, which also makes Postgres, Redis and
   every other in-namespace service reachable from sandboxed agent code.

## Decision

Each sandbox gets its own throwaway namespace, `devai-sbx-<uuid>`, created at
provision and deleted wholesale at destroy/expiry. Same node pool as today —
no gVisor pool yet, but the runtime class becomes configurable so a kernel
isolation tier later is a values change, not a code change.

Sandbox ids are lowercase UUID4 (36 chars), so the namespace name is 46 chars,
within the 63-char RFC1123 limit, with no sanitisation needed.

## Namespace lifecycle

**Provision** (`SandboxProvisioner.provision`), in order:

1. Create Namespace `devai-sbx-<id>` with labels:
   - `app.kubernetes.io/managed-by: devai`
   - `devai.tesserix.app/sandbox: <id>`
   - `devai.tesserix.app/owner-hash: <sha256(owner)[:16]>` (owner may be an
     email; hash keeps it label-safe and out of cluster metadata)
   - `pod-security.kubernetes.io/enforce: restricted` — the kubelet refuses
     privileged/root pods even if a manifest builder regresses.
2. Create the `devai-sandbox` ServiceAccount in the namespace (automount
   disabled) and copy the image pull secret when one is configured.
3. Create the existing manifest set — quota, limits, network policy, sandbox
   Secret, egress proxy, optional workspace — all in the sandbox namespace.

**Teardown** (`SandboxProvisioner.teardown`):

1. Snapshot the workspace (unchanged — must precede any deletion).
2. Revoke all broker grants (unchanged).
3. Delete the namespace. Kubernetes garbage-collects everything inside; the
   per-object delete loop is only kept for legacy records (see Migration).

**Reaper**: the existing expiry sweep gains a namespace pass:

- List namespaces labelled `app.kubernetes.io/managed-by=devai` with prefix
  `devai-sbx-`.
- Delete any whose sandbox row is expired, destroyed or missing (orphans from
  control-plane crashes).
- Re-issue deletion for namespaces stuck `Terminating` beyond a grace period
  and log them; a stuck namespace is an operator signal, not silent state.

## Objects inside the boundary

- **Job / proxy / workspace / Secrets** — same builders, namespace parameter
  now the sandbox namespace. `proxy_service_host` and
  `workspace_service_host` already take `namespace=`; callers pass the
  sandbox one.
- **ResourceQuota / LimitRange** — become plain namespace-wide objects; the
  PriorityClass `scopeSelector` trick (needed only to fence within a shared
  namespace) is removed. Hard limits unchanged (4 pods / 8 CPU / 16 Gi;
  2 CPU / 4 Gi per container).
- **NetworkPolicy** — tightened:
  - Egress: kube-dns (53/UDP+TCP); own namespace (proxy, workspace); devai
    control-plane pods only — `namespaceSelector` on the control-plane
    namespace **and** `podSelector: app.kubernetes.io/name: devai`. Postgres,
    Redis, NATS and everything else in `devai` become unreachable.
  - Ingress: control-plane pods + own namespace. Cross-sandbox traffic is now
    impossible by namespace boundary, independent of label correctness.
- **Pod hardening** — sandbox job container gains
  `readOnlyRootFilesystem: true` with an `emptyDir` mounted at the runner
  work path (closing the writable-rootfs gap). Proxy and workspace pods get
  the same treatment where their images allow it.

## Control-plane changes

### `K8sJobRuntime` (`src/devai/runtime/k8s_client.py`)

- Add `Namespace` and `ServiceAccount` to the manifest dispatch table;
  Namespace is cluster-scoped so it dispatches to
  `create_namespace`/`delete_namespace` rather than the `*_namespaced_*`
  pattern.
- Add `namespace=` overrides to `create_job`, `get_job`, `delete_job`,
  `find_pod_for_job`, `pod_logs`. Default (`None`) keeps today's behaviour
  for non-sandbox jobs.
- Add `list_namespaces(label_selector=...)` for the reaper.

### Record and dispatch

- The sandbox's namespace is stored on the record (`detail["namespace"]`) at
  provision time. Every later actor — job dispatch, invoke, trace, snapshot,
  teardown, reaper — reads the namespace from the record, never derives it.
- `apply_sandbox_boundary` stamps the sandbox namespace onto the Job metadata
  so the runner dispatch path creates it in the right place.
- Env plumbing (`_pinned_env`, `proxy_env`, workspace env) resolves hostnames
  against the record's namespace.

### RuntimeClass (future gVisor tier)

New setting `DEVAI_SANDBOX_RUNTIME_CLASS` (default empty). When set,
`runtimeClassName` is stamped on sandbox Job, workspace and proxy pod specs.
Enabling gVisor later = provision a GKE Sandbox node pool + set the value.
No behavioural change while empty.

### RBAC (lands in `tesserix-k8s`, ArgoCD lane — separate PR)

ClusterRole + ClusterRoleBinding for the devai control-plane ServiceAccount:

- `namespaces`: create, get, list, delete
- `jobs`, `pods`, `pods/log`, `secrets`, `configmaps`, `services`,
  `serviceaccounts`, `networkpolicies`, `resourcequotas`, `limitranges`,
  `persistentvolumeclaims`: full manage, cluster-wide

Follow-up guard (out of scope here, noted for the roadmap): a Kyverno policy
restricting the devai SA's namespace create/delete to the `devai-sbx-` prefix,
since RBAC cannot express name-prefix constraints on create.

## Migration

No flag day. Teardown acts on the namespace recorded at provision time:

- New records carry `detail["namespace"] = devai-sbx-<id>` → namespace delete.
- Old records have no recorded namespace → legacy per-object delete loop in
  the shared namespace, exactly as today.

The legacy path is removed once no live pre-change sandboxes remain (max TTL
is 24 h, so effectively one day after deploy).

## Out of scope (future tiers, in order)

1. Kyverno name-prefix guard on the ClusterRole.
2. gVisor via GKE Sandbox node pool (`DEVAI_SANDBOX_RUNTIME_CLASS=gvisor`).
3. MicroVM tier (Kata / managed E2B) for arbitrary untrusted customer code.
4. Snapshot output scanning (malware / secret detection on workspace capture).

## Testing

Unit:

- Namespace manifest: name, labels (incl. PSA `restricted`), owner hash.
- Provision order: namespace exists before any namespaced object.
- Teardown selects namespace-delete vs legacy path from the record.
- Reaper: orphan namespace (no row) deleted; live one kept; stuck
  `Terminating` re-deleted and logged.
- RuntimeClass stamped when set, absent when not.
- NetworkPolicy egress contains control-plane pod selector and no bare
  namespace-wide allow; `readOnlyRootFilesystem` + emptyDir on the job spec.

Live (local kind via `connect-local`):

- Create sandbox → namespace exists with all objects; workspace reachable
  through control plane; direct sandbox→Postgres connection refused.
- Destroy → namespace gone, nothing labelled with the sandbox id remains.
- Expiry reaper removes an orphaned `devai-sbx-*` namespace.
