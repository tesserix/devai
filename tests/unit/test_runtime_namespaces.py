"""Namespace-scoped operations on K8sJobRuntime.

Per-sandbox namespaces mean the runtime must create/delete Namespace objects
(cluster-scoped) and address Jobs, pods and Secrets in a namespace other than
its own. Non-sandbox callers pass nothing and keep today's behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from devai.runtime.k8s_client import K8sJobRuntime, RuntimeConfig


class _Conflict(Exception):
    status = 409


class _RecordingApi:
    """Records (method, kwargs) and answers with just enough shape."""

    def __init__(self, *, conflict_on_create: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._conflict = conflict_on_create

    def __getattr__(self, name: str) -> Any:
        async def call(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            if name.startswith("create_") and self._conflict:
                raise _Conflict()
            if name == "create_namespaced_job":
                return SimpleNamespace(metadata=SimpleNamespace(name=kwargs["body"]["metadata"]["name"]))
            if name == "list_namespace":
                return SimpleNamespace(items=[{"metadata": {"name": "devai-sbx-a", "labels": {}}}])
            if name == "list_namespaced_pod":
                return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="pod-1"))])
            if name == "read_namespaced_pod_log":
                return "logs"
            return SimpleNamespace()

        return call

    def named(self, method: str) -> list[dict[str, Any]]:
        return [kw for m, kw in self.calls if m == method]


def _runtime(api: _RecordingApi) -> K8sJobRuntime:
    runtime = K8sJobRuntime(
        RuntimeConfig(
            namespace="devai",
            runner_image="img",
            runner_image_per_stack={},
            preview_domain="example.app",
            preview_namespace="devai-previews",
            registry_url="",
            pull_secret_name=None,
            service_account_name="devai-runner",
            default_ttl_seconds=3600,
            default_backoff_limit=0,
            pod_security_context={},
        )
    )
    runtime._core_v1 = api  # noqa: SLF001 — standing in for a connected cluster
    runtime._networking_v1 = api  # noqa: SLF001
    runtime._batch_v1 = api  # noqa: SLF001
    return runtime


@pytest.mark.asyncio
async def test_apply_manifest_namespace_kind_uses_cluster_scope() -> None:
    api = _RecordingApi()
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "devai-sbx-x", "labels": {}}}

    await _runtime(api).apply_manifest(manifest)

    assert api.named("create_namespace") == [{"body": manifest}]


@pytest.mark.asyncio
async def test_apply_manifest_namespace_conflict_falls_back_to_patch() -> None:
    api = _RecordingApi(conflict_on_create=True)
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "devai-sbx-x"}}

    await _runtime(api).apply_manifest(manifest)

    assert api.named("patch_namespace") == [{"name": "devai-sbx-x", "body": manifest}]


@pytest.mark.asyncio
async def test_apply_manifest_service_account() -> None:
    api = _RecordingApi()
    manifest = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "devai-sandbox", "namespace": "devai-sbx-x"}}

    await _runtime(api).apply_manifest(manifest)

    assert api.named("create_namespaced_service_account") == [{"namespace": "devai-sbx-x", "body": manifest}]


@pytest.mark.asyncio
async def test_delete_manifest_namespace_kind() -> None:
    api = _RecordingApi()

    await _runtime(api).delete_manifest("Namespace", "devai-sbx-x")

    assert api.named("delete_namespace") == [{"name": "devai-sbx-x"}]


@pytest.mark.asyncio
async def test_delete_namespace_helper() -> None:
    api = _RecordingApi()

    await _runtime(api).delete_namespace("devai-sbx-x")

    assert api.named("delete_namespace") == [{"name": "devai-sbx-x"}]


@pytest.mark.asyncio
async def test_list_namespaces_returns_plain_dicts() -> None:
    api = _RecordingApi()

    out = await _runtime(api).list_namespaces(label_selector="app.kubernetes.io/managed-by=devai")

    assert out == [{"metadata": {"name": "devai-sbx-a", "labels": {}}}]
    assert api.named("list_namespace") == [{"label_selector": "app.kubernetes.io/managed-by=devai"}]


@pytest.mark.asyncio
async def test_create_job_prefers_manifest_namespace() -> None:
    api = _RecordingApi()
    job = {"metadata": {"name": "job-1", "namespace": "devai-sbx-x"}}

    await _runtime(api).create_job(job)

    assert api.named("create_namespaced_job")[0]["namespace"] == "devai-sbx-x"


@pytest.mark.asyncio
async def test_create_job_defaults_to_config_namespace() -> None:
    api = _RecordingApi()

    await _runtime(api).create_job({"metadata": {"name": "job-1"}})

    assert api.named("create_namespaced_job")[0]["namespace"] == "devai"


@pytest.mark.asyncio
async def test_copy_secret_reapplies_into_target_namespace() -> None:
    api = _RecordingApi()

    await _runtime(api).copy_secret("pull-secret", from_namespace="devai", to_namespace="devai-sbx-x")

    assert api.named("read_namespaced_secret")[0] == {"name": "pull-secret", "namespace": "devai"}
    created = api.named("create_namespaced_secret")[0]
    assert created["namespace"] == "devai-sbx-x"
    assert created["body"]["metadata"]["name"] == "pull-secret"


@pytest.mark.asyncio
async def test_job_and_pod_helpers_accept_namespace_override() -> None:
    api = _RecordingApi()
    runtime = _runtime(api)

    await runtime.get_job("job-1", namespace="devai-sbx-x")
    await runtime.find_pod_for_job("job-1", namespace="devai-sbx-x")
    await runtime.pod_logs("pod-1", namespace="devai-sbx-x")
    await runtime.get_job("job-2")

    assert api.named("read_namespaced_job")[0]["namespace"] == "devai-sbx-x"
    assert api.named("list_namespaced_pod")[0]["namespace"] == "devai-sbx-x"
    assert api.named("read_namespaced_pod_log")[0]["namespace"] == "devai-sbx-x"
    assert api.named("read_namespaced_job")[1]["namespace"] == "devai"
