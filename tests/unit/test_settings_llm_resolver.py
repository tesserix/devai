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
    deps2 = StageDeps(
        config=type("C", (), {"llm_require_user_connector": False})(), llm=sentinel, llm_resolver=_NoneResolver()
    )
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


@pytest.mark.asyncio
async def test_resolver_chains_only_the_users_connected_providers():
    """A user's provider fallback must not borrow a platform credential."""
    from devai.settings.models import Scope
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    class _PlatformSettings:
        llm_provider = "anthropic"
        llm_fallback_provider = "anthropic,openai,groq"
        llm_role_chain_provider = ""
        llm_noop_canned_text = "[noop]"
        llm_require_user_connector = True
        anthropic_api_key = "sk-ant-platform"

    svc = SettingsService(secrets=_MemSecrets())
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="owner@example.com",
        connector_key="llm",
        provider="vertex_gemini",
        prefs={
            "vertex_project": "user-project",
            "vertex_location": "global",
            "vertex_gemini_model": "gemini-2.5-flash",
            "groq_model": "llama-3.3-70b-versatile",
        },
        secret_values={
            "vertex_api_key": "AQ.user-vertex",
            "groq_api_key": "gsk_user-groq",
        },
        updated_by="owner@example.com",
    )

    base = _PlatformSettings()
    overlay = await build_overlay(base, Principal(email="owner@example.com"), svc)
    assert isinstance(overlay, PrincipalSettingsOverlay)
    assert overlay.llm_authorized_providers == ("vertex_gemini", "groq")
    assert overlay.llm_fallback_provider == "groq"

    adapter = await PrincipalLLMResolver(base, svc).resolve_for_email("owner@example.com")
    assert adapter is not None
    assert adapter.provider_name == "vertex_gemini→groq"


@pytest.mark.asyncio
async def test_overlay_persists_explicit_primary_and_fallback_order():
    from devai.settings.models import Scope
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    class _PlatformSettings:
        llm_provider = "anthropic"
        llm_fallback_provider = ""
        anthropic_api_key = "sk-ant-platform"

    svc = SettingsService(secrets=_MemSecrets())
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id="owner@example.com",
        connector_key="llm",
        provider="vertex_gemini",
        prefs={
            "fallback_providers": "anthropic,groq",
            "vertex_project": "user-project",
        },
        secret_values={
            "vertex_api_key": "AQ.user-vertex",
            "anthropic_api_key": "sk-ant-user",
            "groq_api_key": "gsk_user-groq",
        },
        updated_by="owner@example.com",
    )

    overlay = await build_overlay(_PlatformSettings(), Principal(email="owner@example.com"), svc)

    assert isinstance(overlay, PrincipalSettingsOverlay)
    assert overlay.llm_provider == "vertex_gemini"
    assert overlay.llm_authorized_providers == ("vertex_gemini", "anthropic", "groq")
    assert overlay.llm_fallback_provider == "anthropic,groq"


def test_llm_connector_catalog_exposes_ordered_fallback_setting():
    field = next(field for field in CONNECTOR_BY_KEY["llm"].fields if field.key == "fallback_providers")
    assert field.settings_attr == "llm_fallback_provider"
    assert field.secret is False


# ─────────────────────────────────────────────────────────────────────
# role_llm_for_principal — the one LLM-selection policy
# ─────────────────────────────────────────────────────────────────────


class _SentinelLLM:
    provider_name = "sentinel"
    default_model = "m"


def _policy_deps(*, strict: bool, budget: int, has_own: bool, overlay=None):
    from devai.pipeline.interfaces import StageDeps

    class _Cfg:
        llm_provider = "noop"
        llm_noop_canned_text = "[noop]"
        llm_require_user_connector = strict
        llm_trial_token_budget = budget
        redis_url = ""  # trial meter degrades to in-memory

    cfg = _Cfg()

    class _Resolver:
        async def llm_overlay_for_email(self, email):
            return (overlay if has_own else cfg), has_own

        async def resolve_for_email(self, email):
            return None

        async def settings_for_email(self, email):
            return overlay if has_own else cfg

    return StageDeps(config=cfg, llm=_SentinelLLM(), llm_resolver=_Resolver())


@pytest.mark.asyncio
async def test_role_llm_trial_meters_humans_without_connector():
    """Strict mode + no connector → platform chain METERED, not raw."""
    from devai.settings.trial import TrialLLMAdapter

    deps = _policy_deps(strict=True, budget=50_000, has_own=False)
    adapter = await deps.role_llm_for_principal("new.user@example.com", "utility")
    assert isinstance(adapter, TrialLLMAdapter)


@pytest.mark.asyncio
async def test_role_llm_refuses_humans_when_trial_disabled():
    deps = _policy_deps(strict=True, budget=0, has_own=False)
    assert await deps.role_llm_for_principal("new.user@example.com", "utility") is None


@pytest.mark.asyncio
async def test_role_llm_never_meters_system_principals():
    from devai.settings.trial import TrialLLMAdapter

    deps = _policy_deps(strict=True, budget=50_000, has_own=False)
    adapter = await deps.role_llm_for_principal("webhook:tesserix/devai#1", "utility")
    assert adapter is not None and not isinstance(adapter, TrialLLMAdapter)


@pytest.mark.asyncio
async def test_role_llm_user_connector_skips_the_meter():
    """A user WITH their own connector is never trial-wrapped — their own
    keys pay for the call."""
    from devai.settings.trial import TrialLLMAdapter

    class _Overlay:
        llm_provider = "anthropic"
        llm_noop_canned_text = "[noop]"
        overlaid_attrs = ("llm_provider", "anthropic_api_key")
        llm_authorized_providers = ("anthropic",)
        anthropic_api_key = "sk-ant-user-own"

    deps = _policy_deps(strict=True, budget=50_000, has_own=True, overlay=_Overlay())
    adapter = await deps.role_llm_for_principal("owner@example.com", "utility")
    assert adapter is not None and adapter.provider_name == "anthropic"
    assert not isinstance(adapter, TrialLLMAdapter)


@pytest.mark.asyncio
async def test_role_llm_strict_mode_never_borrows_platform_for_broken_connector():
    class _IncompleteOverlay:
        llm_provider = "anthropic"
        llm_noop_canned_text = "[noop]"
        llm_authorized_providers = ("anthropic",)
        overlaid_attrs = ("llm_provider",)
        anthropic_api_key = ""

    deps = _policy_deps(strict=True, budget=50_000, has_own=True, overlay=_IncompleteOverlay())
    assert await deps.role_llm_for_principal("owner@example.com", "utility") is None


def test_role_chain_cache_is_isolated_per_credentials():
    """Two settings objects with DIFFERENT keys must never share a cached
    role chain — that would serve tenant A's API key to tenant B."""
    from devai.adapters.llm.factory import _role_cache_key

    class _A:
        llm_provider = "anthropic"
        anthropic_api_key = "sk-ant-tenant-a"

    class _B:
        llm_provider = "anthropic"
        anthropic_api_key = "sk-ant-tenant-b"

    assert _role_cache_key(_A(), "utility") != _role_cache_key(_B(), "utility")
    # Same credentials → same slot (the cache still deduplicates).
    assert _role_cache_key(_A(), "utility") == _role_cache_key(_A(), "utility")


def test_role_chain_cache_is_isolated_per_vertex_credentials():
    from devai.adapters.llm.factory import _role_cache_key

    class _A:
        llm_provider = "vertex_gemini"
        vertex_project = "shared-project"
        vertex_api_key = "AQ.tenant-a"

    class _B:
        llm_provider = "vertex_gemini"
        vertex_project = "shared-project"
        vertex_api_key = "AQ.tenant-b"

    assert _role_cache_key(_A(), "utility") != _role_cache_key(_B(), "utility")


@pytest.mark.asyncio
async def test_principal_run_fails_clearly_when_trial_exhausted():
    """Legacy ALM agents (now AgentStage, via the shared resolve_principal_run)
    must not silently ride the shared platform keys: a human with no connector
    and a spent trial budget gets a clear, actionable stage failure instead."""
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.principal import resolve_principal_run
    from devai.pipeline.types import DevAITask
    from devai.settings.trial import get_trial_meter

    class _Cfg:
        llm_provider = "noop"
        llm_noop_canned_text = "[noop]"
        llm_require_user_connector = True
        llm_trial_token_budget = 100
        redis_url = ""

    cfg = _Cfg()

    class _Resolver:
        async def llm_overlay_for_email(self, email):
            return cfg, False

    deps = StageDeps(config=cfg, llm_resolver=_Resolver())

    meter = get_trial_meter(cfg)
    await meter.add("spent.user@example.com", 10_000)  # way past the budget

    task = DevAITask(intent="x", blueprint="b", repo="o/r")
    task.triggered_by = "spent.user@example.com"
    # The trial gate fires before any agent is built — a clear stage failure.
    with pytest.raises(RuntimeError, match="Settings"):
        await resolve_principal_run(deps, task, trial_gate=True, stage_name="implement_code")
