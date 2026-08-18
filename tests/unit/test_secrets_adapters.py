"""Contract tests for the secrets adapter family."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from devai.adapters.base import AdapterError
from devai.adapters.secrets import SecretRef, create_secrets_adapter
from devai.adapters.secrets.gcp_sm import sanitize_secret_id
from devai.adapters.secrets.noop import NoopSecretsAdapter


class _S:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_factory_degrades_to_noop_for_unknown():
    a = create_secrets_adapter(_S(secrets_provider="vault"))
    assert isinstance(a, NoopSecretsAdapter)


def test_factory_gcp_without_project_degrades():
    a = create_secrets_adapter(_S(secrets_provider="gcp_sm"))
    assert a.provider_name == "noop"


def test_factory_env_provider():
    a = create_secrets_adapter(_S(secrets_provider="env"))
    assert a.provider_name == "env"


def test_noop_refuses_writes_loudly():
    a = NoopSecretsAdapter()
    assert asyncio.run(a.can_write()) is False
    with pytest.raises(AdapterError):
        asyncio.run(a.set_secret("k", "v"))
    assert asyncio.run(a.get_secret("k")) is None


def test_env_reads_environment(monkeypatch):
    monkeypatch.setenv("devai-secret-x", "val-x")
    a = create_secrets_adapter(_S(secrets_provider="env"))
    assert asyncio.run(a.get_secret("devai-secret-x")) == "val-x"
    assert asyncio.run(a.get_secret(SecretRef(name="MISSING"))) is None
    with pytest.raises(AdapterError):
        asyncio.run(a.set_secret("k", "v"))  # env is read-only


@pytest.mark.parametrize(
    "raw,expected_ok",
    [
        ("devai-user-abc-llm-anthropic_api_key", True),
        ("devai/user@x.com:llm", True),  # sanitized
    ],
)
def test_sanitize_secret_id(raw, expected_ok):
    sid = sanitize_secret_id(raw)
    assert sid
    assert all(c.isalnum() or c in "_-" for c in sid)


def test_secret_ref_roundtrip():
    ref = SecretRef(name="n", provider="gcp_sm", version="3", labels={"a": "b"})
    assert SecretRef.from_dict(ref.to_dict()) == ref
    assert SecretRef.from_dict(None) is None
    assert SecretRef.from_dict({"name": ""}) is None


@pytest.mark.asyncio
async def test_openbao_adapter_brokers_write_and_reads_own_prefix(tmp_path):
    from devai.adapters.secrets.openbao import OpenBaoSecretsAdapter

    bao_token = tmp_path / "bao-token"
    broker_token = tmp_path / "broker-token"
    bao_token.write_text("bao-kubernetes-jwt")
    broker_token.write_text("broker-audience-jwt")
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/internal/v1/workload-secrets/capabilities":
            assert request.headers["authorization"] == "Bearer broker-audience-jwt"
            return httpx.Response(200, json={"write": True})
        if request.method == "PUT" and request.url.path.startswith("/internal/v1/workload-secrets/"):
            assert request.headers["authorization"] == "Bearer broker-audience-jwt"
            assert json.loads(request.content)["value"] == "sk-user"
            return httpx.Response(200, json={"path": "devai/devai-api/owner/key", "version": 4})
        if request.url.path == "/v1/auth/kubernetes/login":
            assert json.loads(request.content) == {"jwt": "bao-kubernetes-jwt", "role": "read-devai-api"}
            return httpx.Response(200, json={"auth": {"client_token": "short-bao-token", "lease_duration": 3600}})
        if request.url.path.startswith("/v1/kv/data/devai/devai-api/"):
            assert request.headers["x-vault-token"] == "short-bao-token"
            return httpx.Response(200, json={"data": {"data": {"value": "sk-user"}, "metadata": {"version": 4}}})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenBaoSecretsAdapter(
        _S(
            secrets_openbao_addr="http://openbao.openbao.svc:8200",
            secrets_openbao_mount="kv",
            secrets_openbao_role="read-devai-api",
            secrets_openbao_auth_mount="kubernetes",
            secrets_openbao_token_file=str(bao_token),
            secrets_broker_url="http://secret-service-api.secret-service.svc:8080",
            secrets_broker_token_file=str(broker_token),
        ),
        client=client,
    )

    assert await adapter.can_write() is True
    ref = await adapter.set_secret(
        "devai-user-tenant-a:user-a-llm-default-anthropic-api-key",
        "sk-user",
        labels={"scope": "user", "scope_id": "tenant-a:user-a"},
    )
    assert ref.provider == "openbao"
    assert ref.name.startswith("devai/devai-api/")
    assert await adapter.get_secret(ref) == "sk-user"
    await adapter.close()
    assert len([request for request in seen if request.url.path == "/v1/auth/kubernetes/login"]) == 1


@pytest.mark.asyncio
async def test_openbao_adapter_soft_deletes_through_broker(tmp_path):
    from devai.adapters.secrets.openbao import OpenBaoSecretsAdapter

    token = tmp_path / "broker-token"
    token.write_text("broker-jwt")
    deleted: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json={"write": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenBaoSecretsAdapter(
        _S(
            secrets_openbao_addr="http://openbao",
            secrets_broker_url="http://secret-service",
            secrets_broker_token_file=str(token),
        ),
        client=client,
    )
    ref = SecretRef(
        name="devai/devai-api/0123456789abcdef0123456789abcdef/llm-key",
        provider="openbao",
    )
    assert await adapter.delete_secret(ref) is True
    assert deleted == ["/internal/v1/workload-secrets/0123456789abcdef0123456789abcdef/llm-key"]
    await adapter.close()
