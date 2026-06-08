"""Runtime exception hierarchy.

Kept in its own module so `devai.runtime` is importable even when the
kubernetes_asyncio SDK isn't installed — callers can `except
RuntimeNotAvailable` without pulling in the cluster client.

The base class is named ``DevaiRuntimeError`` rather than ``RuntimeError``
so it does not shadow the builtin: a stray ``except RuntimeError`` in
callers would otherwise silently fail to catch ``JobDispatchFailed`` /
``RuntimeNotConfigured`` (and conversely catch unrelated stdlib errors).
"""

from __future__ import annotations


class DevaiRuntimeError(Exception):
    """Base for all runtime failures."""


class RuntimeNotAvailable(DevaiRuntimeError):
    """The kubernetes-asyncio SDK isn't importable in this environment.

    Raised by `create_runtime()` when the optional dep is missing. Code
    that depends on Job-runner execution should catch this and fall back
    to in-process execution (or fail fast, depending on the stage).
    """


class RuntimeNotConfigured(DevaiRuntimeError):
    """Kubernetes config can be loaded — neither in-cluster nor via kubeconfig.

    Raised by `K8sJobRuntime.connect()` so callers can degrade to in-process
    execution without crashing the whole pipeline service.
    """


class JobDispatchFailed(DevaiRuntimeError):
    """The Kubernetes API rejected the create request.

    Includes the original API error message for triage."""


class JobNotFound(DevaiRuntimeError):
    """No Job matches the given name in the target namespace.

    Raised by `wait_for_completion()` when the Job has been deleted
    out-of-band before reaching a terminal state."""


# Backward-compat alias. Some older call sites (and the package __init__)
# imported the base as ``RuntimeError``; keep the name resolvable while new
# code migrates to ``DevaiRuntimeError``. NOTE: this binds the module-level
# name only — it does NOT shadow the builtin in other modules.
RuntimeError = DevaiRuntimeError  # noqa: A001 — intentional compat alias


__all__ = [
    "DevaiRuntimeError",
    "JobDispatchFailed",
    "JobNotFound",
    "RuntimeError",
    "RuntimeNotAvailable",
    "RuntimeNotConfigured",
]
