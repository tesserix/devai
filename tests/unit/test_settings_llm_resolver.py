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
