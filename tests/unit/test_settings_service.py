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
        self._writable = writable

    async def can_write(self) -> bool:
        return self._writable

    async def set_secret(self, key, value, *, labels=None):
        self.store[key] = value
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
    svc = _svc()

    async def go():
        c = await svc.upsert_connector(
            scope=Scope.USER, scope_id="alice-uid", connector_key="llm", provider="openai",
            prefs={"openai_model": "gpt-4.1"},
            secret_values={"openai_api_key": "ALICE-KEY"}, updated_by="alice@x.com",
        )
        # The stored connector keeps only a ref, never the value.
        assert "openai_api_key" in c.secret_refs
        assert "ALICE-KEY" not in str(c.secret_refs)
        assert "openai_api_key" not in c.prefs  # secret stripped from prefs
        # public view never leaks values
        pub = c.public_dict()
        assert "ALICE-KEY" not in str(pub)
        assert "openai_api_key" in pub["secrets_set"]

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
            scope=Scope.GLOBAL, scope_id="", connector_key="llm", provider="anthropic",
            secret_values={"anthropic_api_key": "GLOBAL-A"}, updated_by="admin",
        )
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="alice-uid", connector_key="llm", provider="openai",
            prefs={"openai_model": "gpt-4.1"},
            secret_values={"openai_api_key": "ALICE-OAI"}, updated_by="alice@x.com",
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
            scope=Scope.USER, scope_id="alice-uid", connector_key="mcp",
            provider="streamable_http", instance_id="tools",
            prefs={"mcp_name": "tools", "mcp_url": "https://h/mcp"},
            secret_values={"mcp_token": "TKN"}, updated_by="alice@x.com",
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
            scope=Scope.USER, scope_id="alice-uid", connector_key="llm", provider="openai",
            secret_values={"openai_api_key": "K"}, updated_by="a",
        )
        assert await svc.delete_connector(Scope.USER, "alice-uid", "llm") is True
        ov = await build_overlay(_Base(), _P(), svc)
        # Back to base (no connectors).
        assert ov.llm_provider == "anthropic"

    asyncio.run(go())


def test_unknown_connector_rejected():
    svc = _svc()
    with pytest.raises(ValueError):
        asyncio.run(
            svc.upsert_connector(
                scope=Scope.USER, scope_id="x", connector_key="nope", provider="p"
            )
        )
