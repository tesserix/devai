"""JobWatcher across per-sandbox namespaces.

Sandbox Jobs now run in `devai-sbx-*` namespaces, so the watch must be
cluster-wide and the poll/log paths must address the Job's own namespace.
"""

from __future__ import annotations

from typing import Any

import pytest

from devai.runtime.job_watcher import JobWatcher


class _FakeBatchV1:
    async def list_job_for_all_namespaces(self, **kwargs: Any) -> None:
        return None

    async def list_namespaced_job(self, **kwargs: Any) -> None:
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.batch_v1 = _FakeBatchV1()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_job(self, name: str, *, namespace: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_job", {"name": name, "namespace": namespace}))
        return _terminal_job(name, namespace or "devai")

    async def find_pod_for_job(self, job_name: str, *, namespace: str | None = None) -> str:
        self.calls.append(("find_pod_for_job", {"job_name": job_name, "namespace": namespace}))
        return "pod-1"

    async def pod_logs(self, pod_name: str, *, namespace: str | None = None, tail_lines: int = 500) -> str:
        self.calls.append(("pod_logs", {"pod_name": pod_name, "namespace": namespace}))
        return ""

    def named(self, method: str) -> list[dict[str, Any]]:
        return [kw for m, kw in self.calls if m == method]


def _terminal_job(name: str, namespace: str) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "status": {"conditions": [{"type": "Complete", "status": "True", "message": "done"}]},
    }


def test_watch_uses_all_namespaces_listing() -> None:
    runtime = _FakeRuntime()
    fn, kwargs = JobWatcher(runtime)._stream_args()  # noqa: SLF001

    assert fn == runtime.batch_v1.list_job_for_all_namespaces  # bound methods compare by target
    assert "namespace" not in kwargs
    assert kwargs["label_selector"] == "devai.tesserix.app/role=runner"


@pytest.mark.asyncio
async def test_poll_once_passes_namespace() -> None:
    runtime = _FakeRuntime()

    assert await JobWatcher(runtime).poll_once("job-x", namespace="devai-sbx-y") is True

    assert runtime.named("get_job") == [{"name": "job-x", "namespace": "devai-sbx-y"}]


@pytest.mark.asyncio
async def test_process_job_reads_logs_from_job_namespace() -> None:
    runtime = _FakeRuntime()

    terminal = await JobWatcher(runtime)._process_job(_terminal_job("job-x", "devai-sbx-y"))  # noqa: SLF001

    assert terminal is True
    assert runtime.named("find_pod_for_job") == [{"job_name": "job-x", "namespace": "devai-sbx-y"}]
    assert runtime.named("pod_logs") == [{"pod_name": "pod-1", "namespace": "devai-sbx-y"}]
