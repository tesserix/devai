"""Tests for SettingsService (in-memory store) + the PrincipalSettingsOverlay."""

from __future__ import annotations

import asyncio

import pytest

from devai.adapters.secrets.base import SecretRef, SecretsAdapter
from devai.settings.models import Scope
from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay
from devai.settings.service import SettingsService


class _FakeSecrets(SecretsAdapter):
    provider_name = "fake"

    def __init__(self, writable: bool = True) -> None:
        self.store: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self._writable = writable

    async def can_write(self) -> bool:
        return self._writable

    async def set_secret(self, key, value, *, labels=None):
        self.store[key] = value
        self.labels = dict(labels or {})
        return SecretRef(name=key, provider="fake")

    async def get_secret(self, ref):
        n = ref.name if isinstance(ref, SecretRef) else ref
        return self.store.get(n)

    async def delete_secret(self, ref):
        n = ref.name if isinstance(ref, SecretRef) else ref
        self.store.pop(n, None)
        return True


class _Base:
    llm_provider = "anthropic"
    anthropic_api_key = "GLOBAL"
    claude_model = "global-model"
    openai_api_key = "GLOBAL-OAI"
    openai_model = "gpt-x"
    scm_provider = "github"
    scm_token = "GLOBAL-SCM"


class _P:
    email = "alice@x.com"
    uid = "alice-uid"
    tenant_id = "t1"
    team_ids = ["teamA"]


def _svc(writable=True):
    return SettingsService(pool=None, secrets=_FakeSecrets(writable))


def test_upsert_stores_secret_ref_not_value():
    secrets = _FakeSecrets()
    svc = SettingsService(pool=None, secrets=secrets)

    async def go():
        c = await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="alice-uid",
            connector_key="llm",
            provider="openai",
            prefs={"openai_model": "gpt-4.1"},
            secret_values={"openai_api_key": "ALICE-KEY"},
            updated_by="alice@x.com",
        )
        # The stored connector keeps only a ref, never the value.
        assert "openai_api_key" in c.secret_refs
        assert "ALICE-KEY" not in str(c.secret_refs)
        assert "openai_api_key" not in c.prefs  # secret stripped from prefs
        # public view never leaks values
        pub = c.public_dict()
        assert "ALICE-KEY" not in str(pub)
        assert "openai_api_key" in pub["secrets_set"]
        assert secrets.labels["scope_id"] == "alice-uid"

    asyncio.run(go())


def test_secret_writes_blocked_when_readonly():
    svc = _svc(writable=False)

    async def go():
        assert await svc.secrets_writable() is False
        # The route layer guards this, but the service still records the ref via
        # set_secret — here we assert can_write surfaces the read-only state.

    asyncio.run(go())


def test_overlay_scope_resolution_user_wins():
    svc = _svc()

    async def go():
        await svc.upsert_connector(
            scope=Scope.GLOBAL,
            scope_id="",
            connector_key="llm",
            provider="anthropic",
            secret_values={"anthropic_api_key": "GLOBAL-A"},
            updated_by="admin",
        )
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="t1:alice-uid",
            connector_key="llm",
            provider="openai",
            prefs={"openai_model": "gpt-4.1"},
            secret_values={"openai_api_key": "ALICE-OAI"},
            updated_by="alice@x.com",
        )
        ov = await build_overlay(_Base(), _P(), svc)
        assert isinstance(ov, PrincipalSettingsOverlay)
        assert ov.llm_provider == "openai"  # user wins over global
        assert ov.openai_api_key == "ALICE-OAI"
        assert ov.openai_model == "gpt-4.1"
        assert ov.scm_token == "GLOBAL-SCM"  # untouched falls through

    asyncio.run(go())


def test_overlay_falls_back_to_base_when_no_connectors():
    svc = _svc()

    async def go():
        ov = await build_overlay(_Base(), _P(), svc)
        # No connectors → base settings returned unchanged.
        assert ov is not None
        assert ov.llm_provider == "anthropic"

    asyncio.run(go())


def test_overlay_collects_mcp_servers():
    svc = _svc()

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="t1:alice-uid",
            connector_key="mcp",
            provider="streamable_http",
            instance_id="tools",
            prefs={"mcp_name": "tools", "mcp_url": "https://h/mcp"},
            secret_values={"mcp_token": "TKN"},
            updated_by="alice@x.com",
        )
        ov = await build_overlay(_Base(), _P(), svc)
        assert isinstance(ov, PrincipalSettingsOverlay)
        assert ov.mcp_servers[0]["url"] == "https://h/mcp"
        assert ov.mcp_servers[0]["token"] == "TKN"

    asyncio.run(go())


def test_delete_removes_secret():
    svc = _svc()

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="t1:alice-uid",
            connector_key="llm",
            provider="openai",
            secret_values={"openai_api_key": "K"},
            updated_by="a",
        )
        assert await svc.delete_connector(Scope.USER, "t1:alice-uid", "llm") is True
        ov = await build_overlay(_Base(), _P(), svc)
        # Back to base (no connectors).
        assert ov.llm_provider == "anthropic"

    asyncio.run(go())


@pytest.mark.asyncio
async def test_same_subject_in_two_tenants_resolves_only_own_connector():
    from devai.identity import Principal

    svc = _svc()
    for tenant, key in (("tenant-a", "KEY-A"), ("tenant-b", "KEY-B")):
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id=f"{tenant}:shared-uid",
            connector_key="llm",
            provider="openai",
            secret_values={"openai_api_key": key},
            updated_by="same@example.com",
        )

    overlay_a = await build_overlay(
        _Base(), Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-a"), svc
    )
    overlay_b = await build_overlay(
        _Base(), Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-b"), svc
    )

    assert overlay_a.openai_api_key == "KEY-A"
    assert overlay_b.openai_api_key == "KEY-B"


def test_unknown_connector_rejected():
    svc = _svc()
    with pytest.raises(ValueError):
        asyncio.run(svc.upsert_connector(scope=Scope.USER, scope_id="x", connector_key="nope", provider="p"))


@pytest.mark.asyncio
async def test_upsert_writes_audit_with_no_secret_values():
    """Audit records actor + field names, never secret values."""
    rows = []

    class _Pool:
        async def execute(self, sql, *args):
            rows.append((sql, args))

        async def fetch(self, sql, *args):
            return []

    from devai.settings.models import Scope
    from devai.settings.service import SettingsService

    class _Secrets:
        async def can_write(self):
            return True

        async def set_secret(self, key, value, labels=None):
            class _R:
                name = key

            return _R()

    svc = SettingsService(pool=_Pool(), secrets=_Secrets())
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="uid-9",
        connector_key="llm",
        provider="anthropic",
        secret_values={"anthropic_api_key": "sk-ant-SECRET"},
        updated_by="me@example.com",
    )
    audit = [r for r in rows if "audit_log" in r[0]]
    assert audit, "an audit_log row must be written on upsert"
    flat = str(audit[-1])
    assert "settings.connector.upsert" in flat
    assert "me@example.com" in flat
    assert "sk-ant-SECRET" not in flat  # value never audited
    assert "anthropic_api_key" in flat  # field NAME is fine


def test_resave_merges_prefs_and_keeps_other_provider_keys():
    """Adding OpenAI after Anthropic must keep the Anthropic key AND the
    earlier non-secret prefs (the exact bug the user hit)."""
    svc = _svc()

    async def go():
        # Day 1: Anthropic.
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="u",
            connector_key="llm",
            provider="anthropic",
            prefs={"claude_model": "claude-opus-4-8"},
            secret_values={"anthropic_api_key": "ANT"},
            updated_by="u",
        )
        # Day 2: OpenAI (same default instance).
        c = await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="u",
            connector_key="llm",
            provider="openai",
            prefs={"openai_model": "gpt-4.1"},
            secret_values={"openai_api_key": "OAI"},
            updated_by="u",
        )
        # Both keys survive.
        assert set(c.secret_refs) == {"anthropic_api_key", "openai_api_key"}
        # The earlier non-secret pref is NOT dropped by the second save.
        assert c.prefs["claude_model"] == "claude-opus-4-8"
        assert c.prefs["openai_model"] == "gpt-4.1"

    asyncio.run(go())


def test_clear_one_secret_field_keeps_the_rest():
    svc = _svc()

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id="u",
            connector_key="llm",
            provider="openai",
            secret_values={"anthropic_api_key": "ANT", "openai_api_key": "OAI"},
            updated_by="u",
        )
        ok = await svc.clear_secret_field(Scope.USER, "u", "llm", "anthropic_api_key", actor="u")
        assert ok is True
        rows = await svc.list_connectors(Scope.USER, "u")
        assert set(rows[0].secret_refs) == {"openai_api_key"}  # only anthropic removed
        # clearing a field that isn't set is a no-op
        assert await svc.clear_secret_field(Scope.USER, "u", "llm", "groq_api_key", actor="u") is False

    asyncio.run(go())
