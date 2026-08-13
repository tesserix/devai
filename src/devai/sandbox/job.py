"""Turn a normal runner Job into a sandboxed one.

Written as an overlay rather than a branch inside ``build_job_spec`` so the
claim "a sandbox is the same runtime with different boundaries" is enforced by
construction: whatever the pipeline dispatches, the sandbox runs the same Job
with its edges tightened.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

SANDBOX_LABEL = "devai.tesserix.app/sandbox"

SANDBOX_SERVICE_ACCOUNT = "devai-sandbox"

# Short — a finished sandbox Job has already streamed its trace out.
_JOB_TTL_SECONDS = 300


# Stage-config key the dispatcher reads. Underscore-prefixed keys are internal
# and are not forwarded to the runner as stage env.
SANDBOX_CONFIG_KEY = "__sandbox"


def sandbox_from_stage_config(config: dict[str, Any]) -> SandboxRecord | None:
    """Return the sandbox a stage should run in, if it was dispatched into one."""
    from devai.sandbox.models import SandboxRecord

    blob = config.get(SANDBOX_CONFIG_KEY)
    if not blob:
        return None
    if isinstance(blob, SandboxRecord):
        return blob
    try:
        return SandboxRecord.model_validate(blob)
    except Exception:  # noqa: BLE001 — a malformed pin must not run unsandboxed
        raise ValueError("invalid sandbox on stage config") from None


def apply_sandbox_boundary(job: dict[str, Any], record: SandboxRecord) -> dict[str, Any]:
    """Return a copy of ``job`` fenced into ``record``'s sandbox."""
    job = copy.deepcopy(job)
    spec = record.spec
    sid = record.id

    for meta in (job["metadata"], job["spec"]["template"]["metadata"]):
        meta.setdefault("labels", {})[SANDBOX_LABEL] = sid

    job["spec"]["backoffLimit"] = 0  # a retried eval doubles the cost and blurs the trajectory
    job["spec"]["ttlSecondsAfterFinished"] = _JOB_TTL_SECONDS
    job["spec"]["activeDeadlineSeconds"] = spec.limits.max_wall_clock_s

    pod = job["spec"]["template"]["spec"]
    pod["serviceAccountName"] = SANDBOX_SERVICE_ACCOUNT
    pod["automountServiceAccountToken"] = False
    pod["securityContext"] = {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }

    container = pod["containers"][0]
    container["securityContext"] = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": False,  # the runner writes its working tree
        "capabilities": {"drop": ["ALL"]},
    }
    pinned = _pinned_env(record)
    pinned_names = {e["name"] for e in pinned}
    inherited = [e for e in _rescope_secrets(container.get("env", []), sid) if e["name"] not in pinned_names]
    container["env"] = inherited + pinned
    return job


def _rescope_secrets(env: list[dict[str, Any]], sandbox_id: str) -> list[dict[str, Any]]:
    """Repoint every secret reference at the sandbox's own Secret.

    Production credentials must not be reachable from a sandbox: the agent
    decides at runtime what to call, so "it probably won't use it" is not a
    control. The sandbox Secret is optional — absent means the agent simply has
    no key for that provider.
    """
    out: list[dict[str, Any]] = []
    for entry in env:
        ref = entry.get("valueFrom", {}).get("secretKeyRef")
        if ref is None:
            out.append(entry)
            continue
        out.append(
            {
                "name": entry["name"],
                "valueFrom": {
                    "secretKeyRef": {
                        "name": f"devai-sandbox-{sandbox_id}",
                        "key": ref["key"],
                        "optional": True,
                    }
                },
            }
        )
    return out


def _pinned_env(record: SandboxRecord) -> list[dict[str, Any]]:
    """The spec, flattened into env so the pod cannot resolve it differently."""
    spec = record.spec
    env: list[dict[str, Any]] = [
        {"name": "DEVAI_SANDBOX_ID", "value": record.id},
        {"name": "DEVAI_SANDBOX_OWNER", "value": record.owner},
        {"name": "DEVAI_SANDBOX_AGENT", "value": f"{spec.agent.name}@{spec.agent.version}"},
        {"name": "DEVAI_LLM_PROVIDER", "value": spec.model.provider},
        {"name": "DEVAI_SANDBOX_MODEL", "value": spec.model.model},
        {"name": "DEVAI_SANDBOX_TOOL_MODE", "value": spec.tools.default_mode.value},
        {
            "name": "DEVAI_SANDBOX_TOOL_OVERRIDES",
            "value": json.dumps({k: v.value for k, v in spec.tools.overrides.items()}),
        },
        {"name": "DEVAI_SANDBOX_MAX_TOKENS", "value": str(spec.limits.max_tokens)},
        {"name": "DEVAI_SANDBOX_MAX_COST_USD", "value": str(spec.limits.max_cost_usd)},
        {"name": "DEVAI_SANDBOX_MAX_WALL_CLOCK_S", "value": str(spec.limits.max_wall_clock_s)},
    ]
    if spec.prompt is not None:
        env.append({"name": "DEVAI_SANDBOX_PROMPT", "value": f"{spec.prompt.ref}@{spec.prompt.version}"})
    if spec.dataset is not None:
        env.append({"name": "DEVAI_SANDBOX_DATASET", "value": f"{spec.dataset.ref}@{spec.dataset.version}"})
    return env


__all__ = [
    "SANDBOX_CONFIG_KEY",
    "SANDBOX_LABEL",
    "SANDBOX_SERVICE_ACCOUNT",
    "apply_sandbox_boundary",
    "sandbox_from_stage_config",
]
