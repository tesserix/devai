"""Contract tests for the secrets adapter family."""

from __future__ import annotations

import asyncio

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
