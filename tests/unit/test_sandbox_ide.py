"""Human takeover of a running workspace (#206).

The point of the takeover is that nothing is recreated: a person opens the tree
in the state the agent left it. That only holds if the IDE runs *in* the
workspace pod, on the same volume — and if nothing else in the namespace,
including another sandbox, can reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from devai.sandbox.isolation import build_isolation_manifests
from devai.sandbox.job import SANDBOX_LABEL
from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.workspace import IDE_PORT, WORKSPACE_ROOT, build_workspace_manifests


def _record(**spec_extra: Any) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner="dev@example.com",
        spec=SandboxSpec.model_validate(
            {
                "agent": {"name": "dev", "version": "1"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "workspace": True,
                **spec_extra,
            }
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _manifests(record: SandboxRecord) -> dict[str, dict[str, Any]]:
    return {m["kind"]: m for m in build_workspace_manifests(record, namespace="devai", token="tok")}


def test_a_workspace_without_takeover_runs_no_ide() -> None:
    pod = _manifests(_record())["Pod"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["workspace"]


def test_the_ide_edits_the_same_tree_the_agent_left() -> None:
    pod = _manifests(_record(ide=True))["Pod"]["spec"]
    ide = next(c for c in pod["containers"] if c["name"] == "ide")

    assert ide["volumeMounts"] == pod["containers"][0]["volumeMounts"]
    assert WORKSPACE_ROOT in " ".join(ide["args"])


def test_the_ide_is_reachable_on_the_workspace_service() -> None:
    ports = _manifests(_record(ide=True))["Service"]["spec"]["ports"]
    assert IDE_PORT in [p["port"] for p in ports]


def test_no_other_sandbox_can_reach_this_one() -> None:
    """The IDE trusts whoever reaches it, so only the control plane may."""
    policy = next(m for m in build_isolation_manifests(_record(ide=True), namespace="devai") if m["kind"] == "NetworkPolicy")

    assert "Ingress" in policy["spec"]["policyTypes"]
    allowed = policy["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "devai"}}} in allowed
    assert all(SANDBOX_LABEL not in str(rule) for rule in allowed)
