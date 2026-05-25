"""Runtime exception hierarchy.

Kept in its own module so `devai.runtime` is importable even when the
kubernetes_asyncio SDK isn't installed — callers can `except
RuntimeNotAvailable` without pulling in the cluster client.
"""

from __future__ import annotations


class RuntimeError(Exception):
    """Base for all runtime failures."""


class RuntimeNotAvailable(RuntimeError):
    """The kubernetes-asyncio SDK isn't importable in this environment.

    Raised by `create_runtime()` when the optional dep is missing. Code
    that depends on Job-runner execution should catch this and fall back
    to in-process execution (or fail fast, depending on the stage).
    """


class RuntimeNotConfigured(RuntimeError):
    """Kubernetes config can be loaded — neither in-cluster nor via kubeconfig.

    Raised by `K8sJobRuntime.connect()` so callers can degrade to in-process
    execution without crashing the whole pipeline service.
    """


class JobDispatchFailed(RuntimeError):
    """The Kubernetes API rejected the create request.

    Includes the original API error message for triage."""


class JobNotFound(RuntimeError):
    """No Job matches the given name in the target namespace.

    Raised by `wait_for_completion()` when the Job has been deleted
    out-of-band before reaching a terminal state."""


__all__ = [
    "JobDispatchFailed",
    "JobNotFound",
    "RuntimeError",
    "RuntimeNotAvailable",
    "RuntimeNotConfigured",
]
