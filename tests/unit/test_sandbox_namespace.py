"""The sandbox namespace builders — the boundary object itself."""

from datetime import UTC, datetime

from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.namespace import (
    build_namespace_manifest,
    build_service_account_manifest,
    recorded_namespace,
    sandbox_namespace,
)

_SID = "0f9b2c1e-1111-2222-3333-444455556666"


def _record(detail: dict | None = None) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id=_SID,
        owner="samyak.rout@gmail.com",
        spec=SandboxSpec(agent=AgentRef(name="a", version="1"), model=ModelRef(provider="p", model="m")),
        status=SandboxStatus.PENDING,
        created_at=now,
        expires_at=now,
        detail=detail or {},
    )


def test_sandbox_namespace_name():
    assert sandbox_namespace(_SID) == f"devai-sbx-{_SID}"
    assert len(sandbox_namespace(_SID)) <= 63


def test_namespace_manifest_labels():
    m = build_namespace_manifest(_record())
    assert m["kind"] == "Namespace"
    assert m["metadata"]["name"] == f"devai-sbx-{_SID}"
    labels = m["metadata"]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == "devai"
    assert labels["devai.tesserix.app/sandbox"] == _SID
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    # Owner hash is stable, short and never the raw email.
    assert len(labels["devai.tesserix.app/owner-hash"]) == 16
    assert "@" not in labels["devai.tesserix.app/owner-hash"]
    assert labels["devai.tesserix.app/owner-hash"] == build_namespace_manifest(_record())["metadata"]["labels"][
        "devai.tesserix.app/owner-hash"
    ]


def test_service_account_manifest():
    sa = build_service_account_manifest("devai-sbx-x")
    assert sa["kind"] == "ServiceAccount"
    assert sa["metadata"]["name"] == "devai-sandbox"
    assert sa["metadata"]["namespace"] == "devai-sbx-x"
    assert sa["automountServiceAccountToken"] is False


def test_recorded_namespace():
    assert recorded_namespace(_record()) == ""  # legacy record, shared-namespace teardown
    assert recorded_namespace(_record(detail={"namespace": "devai-sbx-abc"})) == "devai-sbx-abc"
