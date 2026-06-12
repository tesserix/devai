"""Per-principal LLM resolution + tenant isolation guarantees."""

from __future__ import annotations

import pytest

from devai.identity import Principal
from devai.settings.llm_resolver import PrincipalLLMResolver
from devai.settings.models import CONNECTOR_BY_KEY
from devai.settings.service import SettingsService


class _Settings:
    """Minimal stand-in for the global Settings object."""

    llm_provider = "noop"
    llm_noop_canned_text = "[noop]"


class _MemSecrets:
    """In-memory secrets backend implementing the surface the service uses."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def can_write(self) -> bool:
        return True

    async def set_secret(self, key, value, labels=None):
        self.values[key] = value

        class _Ref:
            name = key

        return _Ref()

    async def get_secret(self, ref):
        return self.values.get(ref)

    async def delete_secret(self, ref):
        return self.values.pop(ref, None) is not None


def test_llm_catalog_covers_vertex_and_gateway():
    spec = CONNECTOR_BY_KEY["llm"]
    assert "vertex_gemini" in spec.providers and "gateway" in spec.providers
    attrs = {f.settings_attr for f in spec.fields}
    for needed in ("vertex_project", "vertex_api_key", "vertex_base_url", "llm_gateway_base_url"):
        assert needed in attrs
    # Every key-like field must be marked secret (never stored in PG).
    secret_attrs = {f.settings_attr for f in spec.fields if f.secret}
    assert {"anthropic_api_key", "openai_api_key", "vertex_api_key", "llm_gateway_api_key"} <= secret_attrs


@pytest.mark.asyncio
async def test_resolver_none_when_user_configured_nothing():
    svc = SettingsService(secrets=_MemSecrets())
    resolver = PrincipalLLMResolver(_Settings(), svc)
    assert await resolver.resolve(Principal(email="nobody@example.com")) is None
    assert await resolver.resolve(None) is None
    assert await resolver.resolve_for_email("not-an-email") is None


@pytest.mark.asyncio
async def test_resolver_builds_user_adapter_and_isolates_tenants():
    secrets = _MemSecrets()
    svc = SettingsService(secrets=secrets)

    # User A configures their own Vertex connector (API-key mode — buildable
    # in tests without google-auth).
    from devai.settings.models import Scope

    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="a@example.com",
        connector_key="llm",
        provider="vertex_gemini",
        prefs={"vertex_project": "proj-a", "vertex_location": "global"},
        secret_values={"vertex_api_key": "AQ.user-a"},
        updated_by="a@example.com",
    )

    resolver = PrincipalLLMResolver(_Settings(), svc)

    adapter_a = await resolver.resolve_for_email("a@example.com")
    assert adapter_a is not None
    # InstrumentedLLMAdapter delegates provider_name to the inner backend.
    assert adapter_a.provider_name == "vertex_gemini"

    # User B configured nothing → platform default (None ⇒ deps.llm).
    assert await resolver.resolve_for_email("b@example.com") is None

    # A's secret value never leaks into the public view.
    rows = await svc.list_connectors(Scope.USER, "a@example.com")
    public = rows[0].public_dict()
    assert "AQ.user-a" not in str(public)
    assert public["secrets_set"] == ["vertex_api_key"]

    # The provisioned secret is namespaced per user.
    assert any("user-a@example.com" in k or "a@example.com" in k for k in secrets.values)


@pytest.mark.asyncio
async def test_resolver_caches_by_fingerprint():
    secrets = _MemSecrets()
    svc = SettingsService(secrets=secrets)
    from devai.settings.models import Scope

    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="a@example.com",
        connector_key="llm",
        provider="vertex_gemini",
        prefs={"vertex_project": "proj-a"},
        secret_values={"vertex_api_key": "AQ.user-a"},
    )
    resolver = PrincipalLLMResolver(_Settings(), svc)
    one = await resolver.resolve_for_email("a@example.com")
    two = await resolver.resolve_for_email("a@example.com")
    assert one is two


def test_resolve_spec_provider_aliases():
    from devai.adapters.llm.factory import resolve_spec_provider

    assert resolve_spec_provider("claude") == "anthropic"
    assert resolve_spec_provider("CODEX") == "openai"
    assert resolve_spec_provider("gemini") == "vertex_gemini"
    assert resolve_spec_provider("gateway") == "gateway"
    assert resolve_spec_provider("groq") == "groq"
    assert resolve_spec_provider("openrouter") == "openrouter"
    # No opinion → None (default adapter), never a silent noop:
    assert resolve_spec_provider("auto") is None
    assert resolve_spec_provider("nemoclaw") is None
    assert resolve_spec_provider("") is None


@pytest.mark.asyncio
async def test_settings_for_email_returns_overlay_or_base():
    from devai.settings.models import Scope
    from devai.settings.overlay import PrincipalSettingsOverlay

    svc = SettingsService(secrets=_MemSecrets())
    base = _Settings()
    resolver = PrincipalLLMResolver(base, svc)
    # Nothing configured → base settings back.
    assert await resolver.settings_for_email("nobody@example.com") is base
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="a@example.com",
        connector_key="llm",
        provider="anthropic",
        prefs={"claude_model": "claude-sonnet-4-20250514"},
    )
    overlaid = await resolver.settings_for_email("a@example.com")
    assert isinstance(overlaid, PrincipalSettingsOverlay)


@pytest.mark.asyncio
async def test_overlay_resolves_rows_keyed_by_uid_or_email():
    """Run records only carry the email; rows saved under the GIP uid must
    still resolve (and vice versa)."""
    from devai.settings.models import Scope
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    svc = SettingsService(secrets=_MemSecrets())
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="uid-123",
        connector_key="llm",
        provider="anthropic",
        prefs={"claude_model": "claude-sonnet-4-20250514"},
    )
    overlay = await build_overlay(_Settings(), Principal(email="a@example.com", uid="uid-123"), svc)
    assert isinstance(overlay, PrincipalSettingsOverlay)
    assert overlay.claude_model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_require_user_connector_blocks_humans_not_systems():
    from devai.pipeline.interfaces import StageDeps

    class _Cfg:
        llm_provider = "noop"
        llm_noop_canned_text = "[noop]"
        llm_require_user_connector = True

    class _NoneResolver:
        async def resolve_for_email(self, email):
            return None

    sentinel = object()
    deps = StageDeps(config=_Cfg(), llm=sentinel, llm_resolver=_NoneResolver())
    # Human with no connector → blocked (None), platform key NOT used.
    assert await deps.llm_for_principal("human@example.com") is None
    # Synthetic principals keep the platform adapter.
    assert await deps.llm_for_principal("webhook:tesserix/devai#42") is sentinel
    assert await deps.llm_for_principal("") is sentinel
    # Flag off → platform fallback for everyone.
    deps2 = StageDeps(config=type("C", (), {"llm_require_user_connector": False})(), llm=sentinel, llm_resolver=_NoneResolver())
    assert await deps2.llm_for_principal("human@example.com") is sentinel


@pytest.mark.asyncio
async def test_overlay_bridges_uid_keyed_connector_to_email_at_runtime():
    """The REAL bug: connectors save under the GIP uid, but runs carry only
    the email (triggered_by). The overlay must still resolve the connector."""
    from devai.settings.models import Scope
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    svc = SettingsService(secrets=_MemSecrets())
    # Saved under the UID (as the real Settings route does), updated_by=email.
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="uid-XYZ",
        connector_key="llm",
        provider="anthropic",
        prefs={"claude_model": "claude-opus-4-8"},
        secret_values={"anthropic_api_key": "sk-ant-mine"},
        updated_by="me@example.com",
    )
    # A run only knows the email — Principal(uid="", email=...).
    overlay = await build_overlay(_Settings(), Principal(uid="", email="me@example.com"), svc)
    assert isinstance(overlay, PrincipalSettingsOverlay), "uid-keyed connector must resolve by email"
    assert overlay.llm_provider == "anthropic"
    assert overlay.claude_model == "claude-opus-4-8"
    assert overlay.anthropic_api_key == "sk-ant-mine"
