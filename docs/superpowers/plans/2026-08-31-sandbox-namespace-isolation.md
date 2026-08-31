# Sandbox Namespace Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every sandbox runs in its own throwaway Kubernetes namespace (`devai-sbx-<uuid>`), created at provision and deleted wholesale at destroy/expiry.

**Architecture:** A new `devai.sandbox.namespace` module builds the Namespace + ServiceAccount manifests; the provisioner creates the namespace first and records it on the sandbox row (`detail["namespace"]`); every later actor (job dispatch, watcher, token auth, snapshot, teardown, reaper) reads the namespace from the record. Teardown becomes a single namespace delete; a reaper sweep catches orphans. The JobWatcher moves to a cluster-wide label-selected watch so Jobs in sandbox namespaces still signal completion.

**Tech Stack:** Python 3.12, FastAPI, kubernetes_asyncio, pytest; Helm for RBAC.

**Spec:** `docs/superpowers/specs/2026-08-31-sandbox-namespace-isolation-design.md`

## Global Constraints

- Namespace name: `devai-sbx-<sandbox uuid>` (46 chars, RFC1123-safe; ids are lowercase uuid4).
- Namespace labels: `app.kubernetes.io/managed-by: devai`, `devai.tesserix.app/sandbox: <id>`, `devai.tesserix.app/owner-hash: sha256(owner)[:16]`, `pod-security.kubernetes.io/enforce: restricted`.
- No behavioural change for non-sandbox jobs: every new `namespace=` parameter defaults to `None` → runtime-config namespace.
- Migration: records without `detail["namespace"]` use the legacy shared-namespace teardown path.
- Run all commands from the repo root. Test with `pytest tests/unit/<file> -v`; lint with `ruff check src/ tests/` before each commit.
- Commit messages: conventional, no AI references, no Co-Authored-By.

---

### Task 1: `devai.sandbox.namespace` module

**Files:**
- Create: `src/devai/sandbox/namespace.py`
- Test: `tests/unit/test_sandbox_namespace.py`

**Interfaces:**
- Produces: `sandbox_namespace(sandbox_id: str) -> str`; `recorded_namespace(record) -> str` (empty string when the record predates namespaces); `build_namespace_manifest(record) -> dict`; `build_service_account_manifest(namespace: str) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/unit/test_sandbox_namespace.py"""
from datetime import UTC, datetime

from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.namespace import (
    build_namespace_manifest,
    build_service_account_manifest,
    recorded_namespace,
    sandbox_namespace,
)


def _record(detail: dict | None = None) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="0f9b2c1e-1111-2222-3333-444455556666",
        owner="samyak.rout@gmail.com",
        spec=SandboxSpec(agent=AgentRef(name="a", version="1"), model=ModelRef(provider="p", model="m")),
        status=SandboxStatus.PENDING,
        created_at=now,
        expires_at=now,
        detail=detail or {},
    )


def test_sandbox_namespace_name():
    assert sandbox_namespace(_record().id) == "devai-sbx-0f9b2c1e-1111-2222-3333-444455556666"
    assert len(sandbox_namespace(_record().id)) <= 63


def test_namespace_manifest_labels():
    m = build_namespace_manifest(_record())
    assert m["kind"] == "Namespace"
    labels = m["metadata"]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == "devai"
    assert labels["devai.tesserix.app/sandbox"] == _record().id
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    # owner hash is stable, hex, 16 chars, and not the raw email
    assert len(labels["devai.tesserix.app/owner-hash"]) == 16
    assert "@" not in labels["devai.tesserix.app/owner-hash"]


def test_service_account_manifest():
    sa = build_service_account_manifest("devai-sbx-x")
    assert sa["kind"] == "ServiceAccount"
    assert sa["metadata"]["name"] == "devai-sandbox"
    assert sa["metadata"]["namespace"] == "devai-sbx-x"
    assert sa["automountServiceAccountToken"] is False


def test_recorded_namespace():
    assert recorded_namespace(_record()) == ""  # legacy record
    assert recorded_namespace(_record(detail={"namespace": "devai-sbx-abc"})) == "devai-sbx-abc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_sandbox_namespace.py -v`
Expected: FAIL — `ModuleNotFoundError: devai.sandbox.namespace`

- [ ] **Step 3: Implement the module**

```python
"""src/devai/sandbox/namespace.py

The sandbox's own namespace: the boundary is the namespace object itself,
so isolation no longer depends on every label selector being right.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from devai.sandbox.job import SANDBOX_LABEL, SANDBOX_SERVICE_ACCOUNT

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

_PREFIX = "devai-sbx-"


def sandbox_namespace(sandbox_id: str) -> str:
    return f"{_PREFIX}{sandbox_id}"


def recorded_namespace(record: SandboxRecord) -> str:
    """The namespace this sandbox was provisioned into; '' for legacy records."""
    return str((record.detail or {}).get("namespace") or "")


def build_namespace_manifest(record: SandboxRecord) -> dict[str, Any]:
    # Owner may be an email; a short hash keeps it label-safe and out of
    # cluster metadata while staying attributable via the sandbox row.
    owner_hash = hashlib.sha256(record.owner.encode()).hexdigest()[:16]
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": sandbox_namespace(record.id),
            "labels": {
                "app.kubernetes.io/managed-by": "devai",
                SANDBOX_LABEL: record.id,
                "devai.tesserix.app/owner-hash": owner_hash,
                # The kubelet refuses privileged/root pods here even if a
                # manifest builder regresses.
                "pod-security.kubernetes.io/enforce": "restricted",
            },
        },
    }


def build_service_account_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": SANDBOX_SERVICE_ACCOUNT,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "devai"},
        },
        "automountServiceAccountToken": False,
    }


__all__ = [
    "build_namespace_manifest",
    "build_service_account_manifest",
    "recorded_namespace",
    "sandbox_namespace",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_sandbox_namespace.py -v` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/devai/sandbox/namespace.py tests/unit/test_sandbox_namespace.py
git commit -m "feat(sandbox): namespace and service-account manifest builders"
```

---

### Task 2: `K8sJobRuntime` namespace support

**Files:**
- Modify: `src/devai/runtime/k8s_client.py`
- Test: `tests/unit/test_sandbox_runtime.py` (append; follow its existing fake-API style)

**Interfaces:**
- Consumes: nothing new.
- Produces: `apply_manifest`/`delete_manifest` handle `Namespace` (cluster-scoped) and `ServiceAccount`; `delete_namespace(name: str) -> None`; `list_namespaces(label_selector: str) -> list[dict]` (each dict has `metadata.name`, `metadata.labels`, `status.phase`); `create_job(job_spec, namespace: str | None = None)` — explicit arg wins, else `job_spec["metadata"]["namespace"]`, else config namespace; `get_job(name, namespace=None)`, `delete_job(name, namespace=None, ...)`, `find_pod_for_job(job_name, namespace=None)`, `pod_logs(pod_name, namespace=None, tail_lines=500)`.

- [ ] **Step 1: Write the failing tests** — using the same `_FakeCoreV1`/stub pattern already in `tests/unit/test_sandbox_runtime.py`. Cover:

```python
async def test_apply_manifest_namespace_kind_uses_cluster_scope():
    # apply_manifest({"kind": "Namespace", ...}) must call core_v1.create_namespace(body=...),
    # NOT create_namespaced_namespace; 409 falls back to patch_namespace.
    ...

async def test_apply_manifest_service_account():
    # dispatches to create_namespaced_service_account in the manifest's namespace
    ...

async def test_delete_namespace_calls_core_api():
    ...

async def test_create_job_prefers_manifest_namespace():
    # job spec metadata.namespace="devai-sbx-x" → create_namespaced_job(namespace="devai-sbx-x")
    # job spec without namespace → config.namespace
    ...

async def test_pod_helpers_accept_namespace_override():
    # get_job / find_pod_for_job / pod_logs pass namespace through when given
    ...
```

Write these as real tests with fakes recording `(method, namespace)` calls, asserting on the recorded values.

- [ ] **Step 2: Run to verify failure** — `pytest tests/unit/test_sandbox_runtime.py -v` — new tests FAIL (unexpected keyword / missing method).

- [ ] **Step 3: Implement.** In `k8s_client.py`:
  - `_manifest_api`: add `"ServiceAccount": (self._core_v1, "service_account")`.
  - `apply_manifest` / `delete_manifest`: special-case `kind == "Namespace"` before the table — `create_namespace(body=manifest)` (409 → `patch_namespace(name=..., body=manifest)`), `delete_namespace(name=name)`.
  - Add:

```python
async def delete_namespace(self, name: str) -> None:
    await self._core_v1.delete_namespace(name=name)

async def list_namespaces(self, label_selector: str) -> list[dict[str, Any]]:
    out = await self._core_v1.list_namespace(label_selector=label_selector)
    return [item.to_dict() if hasattr(item, "to_dict") else item for item in out.items]
```

  - Thread `namespace: str | None = None` through `create_job`, `get_job`, `delete_job`, `find_pod_for_job`, `pod_logs`; resolve as `namespace or manifest-ns or self._config.namespace` (manifest-ns applies to `create_job` only).

- [ ] **Step 4: Run** `pytest tests/unit/test_sandbox_runtime.py tests/unit/test_runtime_job_spec.py -v` — PASS.

- [ ] **Step 5: Commit** — `feat(runtime): namespace-scoped job and manifest operations`

---

### Task 3: Isolation manifests — namespace-wide quota, tightened NetworkPolicy

**Files:**
- Modify: `src/devai/sandbox/isolation.py`
- Test: `tests/unit/test_sandbox.py` (existing isolation tests live here — update assertions)

**Interfaces:**
- Produces: `build_isolation_manifests(record, *, namespace: str, control_plane_namespace: str = "devai")` — same return shape; quota/limits lose the PriorityClass `scopeSelector`; NetworkPolicy egress/ingress reference the control plane by namespace **and** pod selector.

- [ ] **Step 1: Update/add failing tests**

```python
def test_isolation_quota_has_no_scope_selector():
    quota = build_isolation_manifests(record, namespace="devai-sbx-x")[0]
    assert "scopeSelector" not in quota["spec"]

def test_network_policy_egress_targets_control_plane_pods_only():
    policy = build_isolation_manifests(record, namespace="devai-sbx-x")[2]
    rules = policy["spec"]["egress"]
    # rule 1: kube-dns unchanged; rule 2: own namespace; rule 3: devai control-plane pods
    own = rules[1]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
    assert own == "devai-sbx-x"
    cp = rules[2]["to"][0]
    assert cp["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "devai"
    assert cp["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "devai"

def test_network_policy_ingress_control_plane_and_own_namespace():
    policy = build_isolation_manifests(record, namespace="devai-sbx-x")[2]
    frm = policy["spec"]["ingress"][0]["from"]
    assert {"podSelector": {}} in frm  # own namespace (policy is namespaced now)
    cp = next(e for e in frm if "namespaceSelector" in e)
    assert cp["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "devai"
```

- [ ] **Step 2: Run** — updated tests FAIL against current shapes.

- [ ] **Step 3: Implement.** `_egress_rules(namespace, control_plane_namespace)` returns: DNS rule (unchanged); own-namespace rule (`kubernetes.io/metadata.name: <sandbox ns>`); control-plane rule (`namespaceSelector` on `control_plane_namespace` + `podSelector` `app.kubernetes.io/name: devai`). `_ingress_rules` returns one rule: `{"from": [{"namespaceSelector": {...devai...}, "podSelector": {...devai...}}, {"podSelector": {}}]}` — the empty podSelector means "any pod in this (sandbox) namespace", replacing the old label-based sibling rule. Drop `scopeSelector` from the quota. Verify the control-plane pod label by checking `helm/devai/templates/deployment.yaml` (`app.kubernetes.io/name`); if the chart uses a different label value, use the chart's.

- [ ] **Step 4: Run** `pytest tests/unit/test_sandbox.py -v` — PASS.

- [ ] **Step 5: Commit** — `feat(sandbox): namespace-wide quota and control-plane-only network policy`

---

### Task 4: Boundary overlay — namespace stamping, read-only rootfs, RuntimeClass

**Files:**
- Modify: `src/devai/sandbox/job.py`, `src/devai/config.py`
- Test: `tests/unit/test_sandbox.py` (boundary tests live here)

**Interfaces:**
- Consumes: `recorded_namespace(record)` from Task 1 (import inside function to avoid cycle: `namespace.py` imports from `job.py`).
- Produces: `apply_sandbox_boundary(job, record)` additionally: stamps `job["metadata"]["namespace"]` when the record carries one; sets `readOnlyRootFilesystem: True` + `emptyDir` volume `tmp` at `/tmp` and env `HOME=/devai/work`; stamps `runtimeClassName` from `DEVAI_SANDBOX_RUNTIME_CLASS` when set; resolves proxy/workspace hostnames against the record namespace. New Settings field `sandbox_runtime_class: str = ""` under the sandbox governance block in `config.py`.

- [ ] **Step 1: Write failing tests**

```python
def test_boundary_stamps_record_namespace(record_with_ns, base_job):
    job = apply_sandbox_boundary(base_job, record_with_ns)  # detail={"namespace": "devai-sbx-x"}
    assert job["metadata"]["namespace"] == "devai-sbx-x"
    env = {e["name"]: e.get("value", "") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "devai-sbx-x.svc" in env["HTTP_PROXY"]

def test_boundary_legacy_record_keeps_job_namespace(record_no_ns, base_job):
    job = apply_sandbox_boundary(base_job, record_no_ns)
    assert "namespace" not in job["metadata"]

def test_boundary_readonly_rootfs(record_no_ns, base_job):
    job = apply_sandbox_boundary(base_job, record_no_ns)
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["HOME"] == "/devai/work"

def test_boundary_runtime_class(monkeypatch, record_no_ns, base_job):
    monkeypatch.setenv("DEVAI_SANDBOX_RUNTIME_CLASS", "gvisor")
    job = apply_sandbox_boundary(base_job, record_no_ns)
    assert job["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"
    monkeypatch.delenv("DEVAI_SANDBOX_RUNTIME_CLASS")
    job2 = apply_sandbox_boundary(base_job, record_no_ns)
    assert "runtimeClassName" not in job2["spec"]["template"]["spec"]
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** in `apply_sandbox_boundary` / helpers:
  - `ns = recorded_namespace(record)`; if truthy: `job["metadata"]["namespace"] = ns` and pass `ns` into `_pinned_env(record, namespace=ns)` → `proxy_env(sid, namespace=ns)` and `_workspace_env(record, namespace=ns)` (replace the `os.environ` lookups with the parameter, keeping env-var fallback when the param is empty).
  - Container securityContext: `readOnlyRootFilesystem: True`; append volume `{"name": "tmp", "emptyDir": {}}` and mount `/tmp`; append `{"name": "HOME", "value": "/devai/work"}` to pinned env (the runner's emptyDir workdir — see `job_spec.py`, `/devai/work`).
  - `rc = os.environ.get("DEVAI_SANDBOX_RUNTIME_CLASS", "")`; if set, `pod["runtimeClassName"] = rc`.
  - `config.py`: add `sandbox_runtime_class: str = ""` with a one-line comment in the `# --- Agent sandbox governance ---` block.

- [ ] **Step 4: Run** `pytest tests/unit/test_sandbox.py tests/unit/test_sandbox_egress.py -v` — PASS.

- [ ] **Step 5: Commit** — `feat(sandbox): stamp record namespace, read-only rootfs, runtime class hook`

---

### Task 5: Provisioner — create namespace first, teardown deletes it

**Files:**
- Modify: `src/devai/sandbox/provisioner.py`
- Test: `tests/unit/test_sandbox.py` / `tests/unit/test_sandbox_snapshot.py` (wherever provisioner tests live — `grep -l SandboxProvisioner tests/unit`)

**Interfaces:**
- Consumes: Task 1 builders, Task 2 runtime methods.
- Produces: `provision()` creates Namespace + ServiceAccount before all other manifests, provisions everything into `sandbox_namespace(record.id)`, and includes `"namespace": <ns>` in the READY detail. `teardown()` — records with `recorded_namespace(record)`: snapshot → revoke grants → `delete_namespace(ns)`; legacy records: existing per-object loop, unchanged.

- [ ] **Step 1: Write failing tests** (fake runtime records `apply_manifest`/`delete_namespace` calls in order):

```python
async def test_provision_creates_namespace_first_and_records_it():
    # first applied manifest kind == "Namespace", second == "ServiceAccount";
    # all subsequent manifests carry metadata.namespace == "devai-sbx-<id>";
    # READY detail includes {"namespace": "devai-sbx-<id>"}
    ...

async def test_teardown_namespaced_record_deletes_namespace():
    # record.detail["namespace"] set → runtime.delete_namespace called once;
    # no per-object delete_manifest calls; broker.revoke_all still called;
    # snapshot still taken before delete_namespace
    ...

async def test_teardown_legacy_record_uses_object_loop():
    # record.detail without "namespace" → delete_manifest called per object,
    # delete_namespace never called
    ...
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement.**
  - `provision()`: `ns = sandbox_namespace(record.id)`; apply `build_namespace_manifest(record)` then `build_service_account_manifest(ns)`; pass `namespace=ns` to every existing `build_*` call (isolation, secret, proxy, workspace) and to `read_secret_key`/`WorkspaceClient` host construction; merge `{"namespace": ns}` into the READY `detail`. Copy the image pull secret when `runtime.config.pull_secret_name` is set: read it from the control-plane namespace via `read_namespaced_secret` and re-apply into `ns` (add a small `copy_secret(name, from_ns, to_ns)` helper on the runtime if needed — with a test).
  - `_snapshot()`: `read_secret_key(f"devai-sandbox-ws-{record.id}", "token", namespace=recorded_namespace(record) or namespace)`.
  - `teardown()`: after snapshot + `revoke_all`, `ns = recorded_namespace(record)`; if `ns`: `await self._runtime.delete_namespace(ns)` (wrap in try/except logging "already absent"); else: existing loop verbatim.

- [ ] **Step 4: Run** the provisioner test file — PASS.

- [ ] **Step 5: Commit** — `feat(sandbox): provision into per-sandbox namespace, teardown by namespace delete`

---

### Task 6: JobWatcher cluster-wide watch + namespace-aware polling

**Files:**
- Modify: `src/devai/runtime/job_watcher.py`, `src/devai/pipeline/stages/job_runner.py`
- Test: `tests/unit/test_runtime_job_spec.py` or the watcher's existing test file (`grep -l JobWatcher tests/unit`)

**Interfaces:**
- Consumes: Task 2 (`get_job(name, namespace=)`, `find_pod_for_job(..., namespace=)`, `pod_logs(..., namespace=)`).
- Produces: watch stream switches from `list_namespaced_job` to `list_job_for_all_namespaces` with the same `devai.tesserix.app/role=runner` selector; `poll_once(job_name, namespace: str | None = None)`; `_process_job` reads `metadata.namespace` off the Job object and passes it to `find_pod_for_job`/`pod_logs`. `job_runner.py` passes the sandbox namespace to `poll_once` when the stage runs in a sandbox.

- [ ] **Step 1: Write failing tests**

```python
async def test_watch_uses_all_namespaces_listing():
    # the stream call target is batch_v1.list_job_for_all_namespaces (no namespace kwarg)
    ...

async def test_poll_once_passes_namespace():
    # poll_once("job-x", namespace="devai-sbx-y") → runtime.get_job("job-x", namespace="devai-sbx-y")
    ...

async def test_process_job_reads_logs_from_job_namespace():
    # terminal job dict with metadata.namespace="devai-sbx-y" → find_pod_for_job/pod_logs called with that namespace
    ...
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement.** In `_loop()`: `self._runtime.batch_v1.list_job_for_all_namespaces` with the same label selector (drop the `namespace=` kwarg). RBAC note: cluster-wide job list is granted in Task 7. In `_process_job`: `job_ns = _safe_get(obj, ["metadata", "namespace"])`, pass to the log-tail calls at the failure path (line ~277). `poll_once(self, job_name, namespace=None)` → `self._runtime.get_job(job_name, namespace=namespace)`. In `job_runner.py`, where `poll_once` is called inside the await loop, pass `namespace=job_spec["metadata"].get("namespace")`.

- [ ] **Step 4: Run** watcher + job_runner test files — PASS.

- [ ] **Step 5: Commit** — `feat(runtime): watch sandbox jobs across namespaces`

---

### Task 7: Reaper namespace sweep + record-aware secret reads

**Files:**
- Modify: `src/devai/sandbox/service.py`, `src/devai/sandbox/routes.py`
- Test: `tests/unit/test_sandbox.py` (service tests), `tests/unit/test_sandbox_routes.py`

**Interfaces:**
- Consumes: Task 2 `list_namespaces`/`delete_namespace`; Task 1 `recorded_namespace`.
- Produces: `SandboxService.reap_orphan_namespaces() -> int` called from `_reaper_loop` after `reap_expired()`; `_authorize_sandbox_token(runtime, record, supplied)` (record replaces sandbox_id — secret read in the record's namespace); `_workspace_access` reads the ws token from the record's namespace.

- [ ] **Step 1: Write failing tests**

```python
async def test_reap_orphan_namespaces_deletes_rowless_namespace():
    # runtime lists ns "devai-sbx-dead" labeled managed-by=devai, sandbox row absent
    # → delete_namespace("devai-sbx-dead"); live sandbox's ns survives
    ...

async def test_reap_orphan_namespaces_skips_active_terminating_grace():
    # ns with status.phase == "Terminating" is re-deleted only after being seen twice
    ...

async def test_token_auth_reads_secret_in_record_namespace():
    # record detail namespace devai-sbx-x → read_secret_key(..., namespace="devai-sbx-x")
    ...
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement.**
  - `SandboxService.__init__` gains `runtime: Any | None = None` (wire it where the service is constructed — find with `grep -rn "SandboxService(" src/devai`). Add:

```python
async def reap_orphan_namespaces(self) -> int:
    if self._runtime is None:
        return 0
    reaped = 0
    namespaces = await self._runtime.list_namespaces(
        label_selector="app.kubernetes.io/managed-by=devai,devai.tesserix.app/sandbox"
    )
    for ns in namespaces:
        name = ns["metadata"]["name"]
        sid = ns["metadata"]["labels"].get("devai.tesserix.app/sandbox", "")
        row = await self._db.get_sandbox(sid) if sid else None
        live = row is not None and row["status"] not in ("destroyed", "failed")
        expired = row is not None and row["expires_at"] <= datetime.now(UTC)
        if live and not expired:
            continue
        phase = str((ns.get("status") or {}).get("phase") or "")
        if phase == "Terminating":
            # seen-twice grace: only warn+redelete if it lingered a full sweep
            if name not in self._terminating_seen:
                self._terminating_seen.add(name)
                continue
            logger.warning("sandbox reaper: namespace %s stuck Terminating", name)
        with contextlib.suppress(Exception):
            await self._runtime.delete_namespace(name)
            reaped += 1
        self._terminating_seen.discard(name)
    return reaped
```

    with `self._terminating_seen: set[str] = set()` in `__init__`, and a `reap_orphan_namespaces` call (exception-suppressed) added to `_reaper_loop` beside `reap_expired`.
  - `routes.py`: in `mint_credential`, fetch the record (admin get) *before* token auth; change `_authorize_sandbox_token(runtime, record, supplied)` to `read_secret_key(sandbox_secret_name(record.id), "capability_token", namespace=recorded_namespace(record) or None)`. `_workspace_access`: `read_secret_key(f"devai-sandbox-ws-{record.id}", "token", namespace=recorded_namespace(record) or None)`.

- [ ] **Step 4: Run** `pytest tests/unit/test_sandbox.py tests/unit/test_sandbox_routes.py -v` — PASS.

- [ ] **Step 5: Commit** — `feat(sandbox): orphan-namespace reaper and record-scoped secret reads`

---

### Task 8: Helm RBAC for namespace lifecycle

**Files:**
- Create: `helm/devai/templates/sandbox-rbac.yaml`
- Modify: `helm/devai/values.yaml` (add `sandbox.namespaceIsolation: true` toggle)

**Interfaces:**
- Consumes: the control-plane ServiceAccount name used by `helm/devai/templates/serviceaccount.yaml` (read it; use the chart's helper, e.g. `{{ include "devai.serviceAccountName" . }}`).
- Produces: ClusterRole `devai-sandbox-manager` + ClusterRoleBinding, gated on `.Values.sandbox.namespaceIsolation`.

- [ ] **Step 1: Write the template**

```yaml
{{- if .Values.sandbox.namespaceIsolation }}
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: devai-sandbox-manager
  labels:
    app.kubernetes.io/managed-by: devai
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["create", "get", "list", "watch", "delete", "patch"]
  - apiGroups: [""]
    resources: ["pods", "pods/log", "secrets", "configmaps", "services", "serviceaccounts", "resourcequotas", "limitranges", "persistentvolumeclaims"]
    verbs: ["create", "get", "list", "watch", "delete", "patch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete", "patch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies"]
    verbs: ["create", "get", "list", "watch", "delete", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: devai-sandbox-manager
subjects:
  - kind: ServiceAccount
    name: {{ include "devai.serviceAccountName" . }}
    namespace: {{ .Release.Namespace }}
roleRef:
  kind: ClusterRole
  name: devai-sandbox-manager
  apiGroup: rbac.authorization.k8s.io
{{- end }}
```

(Adjust the SA helper name to whatever `serviceaccount.yaml` actually uses.)

- [ ] **Step 2: Validate** — `helm template helm/devai --set sandbox.namespaceIsolation=true | grep -A5 devai-sandbox-manager` renders both objects; with `=false` renders nothing.

- [ ] **Step 3: Commit** — `feat(helm): cluster rbac for per-sandbox namespaces`

---

### Task 9: Full verification, live kind check, PR

- [ ] **Step 1:** `ruff check src/ tests/ && pytest tests/unit -x -q` — all green.
- [ ] **Step 2:** `connect-local` and verify `kubectl config current-context` is the kind sandbox. Deploy the updated chart/values the way this repo's local lane does (`sandboxctl` values-local — tell the user the exact command if a rebuild/deploy is needed; do not run container builds).
- [ ] **Step 3:** Live checks against `localhost:5050` / the local API:
  - Create a sandbox with `workspace: true` → `kubectl get ns | grep devai-sbx-` shows the namespace; `kubectl get all,netpol,quota,secret -n devai-sbx-<id>` shows the fenced set; PSA label present.
  - From inside the sandbox namespace, a direct connection to Postgres in `devai` fails (e.g. `kubectl exec` the proxy pod: `nc -zw2 <postgres-svc>.devai 5432` → timeout).
  - Destroy the sandbox → namespace gone.
  - Create then delete the DB row manually (or wait for TTL) → reaper removes the orphan namespace within a sweep.
- [ ] **Step 4:** Open a PR to `main` (conventional title, no AI references), wait for CI green, merge with `--squash --admin` per repo workflow.

---

### Task 10: Docs and Pages update

**Files:**
- Modify: `docs/concepts/sandbox-and-evals.md` (isolation section: per-sandbox namespaces, PSA, egress model, runtime-class hook)
- Modify: `docs/PLATFORM-ARCHITECTURE.md` (sandbox boundary description)
- Modify: the GitHub Pages source (`site/` on `origin/main` — use the existing worktree pattern from the Pages revamp; update the sandbox/architecture page + diagram to show `devai-sbx-*` namespaces)

- [ ] **Step 1:** Update both repo docs: describe namespace-per-sandbox lifecycle (create → fenced set → namespace delete), the tightened egress (control-plane pods only, no DB reachability), PSA `restricted`, and the `DEVAI_SANDBOX_RUNTIME_CLASS` forward hook with the gVisor/microVM roadmap from the spec's "Out of scope" list.
- [ ] **Step 2:** Update the Pages site content in the `site/` worktree accordingly and push per the Pages deploy flow used for PR #352.
- [ ] **Step 3:** Commit docs — `docs(sandbox): namespace isolation model` — and verify the Pages deploy rendered.
