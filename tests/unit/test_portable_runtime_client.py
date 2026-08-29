from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from devai.sandbox.models import (
    AgentRef,
    ImportSnapshot,
    ModelRef,
    SandboxLimits,
    SandboxRecord,
    SandboxSpec,
    SandboxStatus,
)
from devai.sandbox.portable_client import PortableRuntimeClient


def _record(runtime: dict[str, object]) -> SandboxRecord:
    now = datetime.now(UTC)
    snapshot = ImportSnapshot(
        import_id="bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09",
        registry_ref="registry://acme/agents/acme/support@1.4.0",
        agent_digest="sha256:" + "a" * 64,
        dependency_lock=[],
        runtime=runtime,
        permissions={},
    )
    return SandboxRecord(
        id="sb-portable",
        owner="tenant-a:alice",
        spec=SandboxSpec(
            agent=AgentRef(name="support", version="1.4.0"),
            import_id=snapshot.import_id,
            import_snapshot=snapshot,
            model=ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
            limits=SandboxLimits(max_wall_clock_s=30),
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


async def test_remote_a2a_uses_bearer_connector_and_extracts_text() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "result": {"message": {"parts": [{"kind": "text", "text": "done"}]}}},
        )

    async def token_provider(record: SandboxRecord) -> str:
        assert record.id == "sb-portable"
        return "short-lived-access-token"

    client = PortableRuntimeClient(
        SimpleNamespace(k8s_runtime_namespace="devai", a2a_allowed_url_suffixes=["*"]),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
        token_provider=token_provider,
    )
    record = _record(
        {
            "type": "remote",
            "protocol": "a2a",
            "url": "https://agents.example.com/a2a/v1",
            "auth": {"type": "bearer", "credentialRef": "openbao://agents/support-token"},
        }
    )

    result = await client.invoke(record, message="triage", triggered_by="tenant-a:alice")

    assert result.final_text == "done"
    assert result.backend == "remote:a2a"
    assert requests[0].url == httpx.URL("https://agents.example.com/a2a/v1")
    assert requests[0].headers["authorization"] == "Bearer short-lived-access-token"
    assert b'"method":"message/send"' in requests[0].content


async def test_authenticated_remote_runtime_fails_closed_without_connector() -> None:
    client = PortableRuntimeClient(
        SimpleNamespace(k8s_runtime_namespace="devai", a2a_allowed_url_suffixes=["*"]),  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": "unexpected"})),
    )
    record = _record(
        {
            "type": "remote",
            "protocol": "http",
            "url": "https://agents.example.com/invoke",
            "auth": {"type": "bearer", "credentialRef": "openbao://agents/support-token"},
        }
    )

    with pytest.raises(ValueError, match="authenticated connector"):
        await client.invoke(record, message="triage", triggered_by="tenant-a:alice")


async def test_container_http_uses_the_pinned_service_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"output": "container result"})

    client = PortableRuntimeClient(
        SimpleNamespace(k8s_runtime_namespace="devai", a2a_allowed_url_suffixes=[]),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    record = _record(
        {
            "type": "container",
            "protocol": "http",
            "image": "ghcr.io/acme/support@sha256:" + "c" * 64,
            "port": 9090,
            "path": "/invoke/v1",
            "healthPath": "/readyz",
        }
    )

    result = await client.invoke(record, message="run", triggered_by="tenant-a:alice")

    assert result.final_text == "container result"
    assert requests[0].url == httpx.URL("http://devai-agent-sb-portable.devai.svc.cluster.local:9090/invoke/v1")
