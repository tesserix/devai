"""Cluster runtime — spawning K8s Jobs per agent run.

This module is the bridge between the in-process blueprint executor and
the cluster. When a blueprint stage runs as a Job (the default once the
runtime is enabled), the executor instantiates a `JobRunnerStage`, which
asks `K8sJobRuntime` to:

  1. Resolve the stage's specialization → agent metadata from the registry.
  2. Materialise a `V1Job` spec (image, env, ConfigMap with task slice).
  3. Submit the Job to the devai namespace.
  4. Wait (async) for completion via the `JobWatcher`, which streams Job
     events and signals stage-bound `asyncio.Event`s.
  5. Read the runner's result from Redis, return it as `StageResult`.

The module is import-safe even when `kubernetes_asyncio` isn't installed
or when the API is not reachable — every public surface degrades to a
no-op stub. Production deployments turn this on via
`DEVAI_K8S_RUNTIME_ENABLED=true`.
"""

from __future__ import annotations

from devai.runtime.errors import (
    JobDispatchFailed,
    JobNotFound,
    RuntimeNotAvailable,
    RuntimeNotConfigured,
)
from devai.runtime.errors import (
    RuntimeError as DevaiRuntimeError,  # alias to avoid stdlib RuntimeError
)
from devai.runtime.job_spec import (
    PreviewInputs,
    RunnerJobInputs,
    build_job_spec,
    build_preview_manifests,
)
from devai.runtime.job_watcher import JobOutcome, JobWatcher
from devai.runtime.k8s_client import K8sJobRuntime, RuntimeConfig, create_runtime

__all__ = [
    "DevaiRuntimeError",
    "JobDispatchFailed",
    "JobNotFound",
    "JobOutcome",
    "JobWatcher",
    "K8sJobRuntime",
    "PreviewInputs",
    "RunnerJobInputs",
    "RuntimeConfig",
    "RuntimeNotAvailable",
    "RuntimeNotConfigured",
    "build_job_spec",
    "build_preview_manifests",
    "create_runtime",
]
