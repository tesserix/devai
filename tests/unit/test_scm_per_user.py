"""Per-user SCM: connector → overlay (PAT or GitHub App) → factory honors it.

Proves a user's Source Control connector delinks them from the platform's
global GitHub App: the overlay carries their creds + the inferred auth method,
and create_scm_client builds a client from the overlay (not the global config).
"""

from __future__ import annotations

import asyncio

import pytest

from devai.scm.factory import _as_int, create_scm_client
from devai.settings.models import Scope
from devai.settings.scm_resolver import PrincipalSCMResolver
from devai.settings.service import SettingsService


class _FakeSecrets:
    provider_name = "fake"

    def __init__(self):
        self.store: dict[str, str] = {}

    async def can_write(self) -> bool:
        return True

    async def set_secret(self, logical, value, labels=None):
        from types import SimpleNamespace

        self.store[logical] = value
        return SimpleNamespace(name=logical)

    async def get_secret(self, name):
        return self.store.get(name)


class _Base:
    """Minimal base settings — the platform's global GitHub App."""

    scm_provider = "github"
    scm_auth_method = "github_app"
    scm_base_url = ""
    scm_token = ""
    scm_organization = ""
    github_app_id = 999  # platform app
    github_app_private_key = "PLATFORM-KEY"
    github_app_installation_id = 111
    github_org = "tesserix"


def test_as_int_coercion():
    assert _as_int("123") == 123 and _as_int(456) == 456 and _as_int("") == 0 and _as_int(None) == 0


# ── overlay infers auth method ──────────────────────────────────────────────


def test_overlay_pat_connector_infers_pat_and_overrides_global():
    from devai.identity import Principal
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="u@x.com", connector_key="scm", provider="github",
            prefs={"scm_organization": "my-org"},
            secret_values={"scm_token": "ghp_USERPAT"}, updated_by="u@x.com",
        )
        overlay = await build_overlay(_Base(), Principal(uid="", email="u@x.com"), svc)
        assert isinstance(overlay, PrincipalSettingsOverlay)
        assert overlay.scm_auth_method == "pat"  # inferred
        assert overlay.scm_token == "ghp_USERPAT"  # the user's, not the platform's
        assert overlay.scm_organization == "my-org"

    asyncio.run(go())


def test_overlay_github_app_connector_infers_app():
    from devai.identity import Principal
    from devai.settings.overlay import build_overlay

    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="u@x.com", connector_key="scm", provider="github",
            prefs={"github_app_id": "424242", "github_app_installation_id": "77"},
            secret_values={"github_app_private_key": "USER-APP-KEY"}, updated_by="u@x.com",
        )
        overlay = await build_overlay(_Base(), Principal(uid="", email="u@x.com"), svc)
        assert overlay.scm_auth_method == "github_app"  # inferred from all-three present
        assert overlay.github_app_id == "424242"  # the user's app, overriding the platform's 999
        assert overlay.github_app_private_key == "USER-APP-KEY"
        assert overlay.github_app_installation_id == "77"

    asyncio.run(go())


# ── factory honors the overlay (per-user creds, not global) ─────────────────


def test_factory_builds_pat_client_from_overlay():
    from devai.identity import Principal
    from devai.settings.overlay import build_overlay

    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="u@x.com", connector_key="scm", provider="github",
            secret_values={"scm_token": "ghp_USERPAT"}, updated_by="u@x.com",
        )
        overlay = await build_overlay(_Base(), Principal(uid="", email="u@x.com"), svc)
        client = create_scm_client(overlay)
        tok = await client._get_token()  # GitHubSCMClient resolves via its transport
        assert tok == "ghp_USERPAT"  # the user's PAT, not a platform App token
        await client.close()

    asyncio.run(go())


def test_factory_app_client_uses_user_app_id():
    from devai.identity import Principal
    from devai.settings.overlay import build_overlay

    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="u@x.com", connector_key="scm", provider="github",
            prefs={"github_app_id": "424242", "github_app_installation_id": "77"},
            secret_values={"github_app_private_key": "USER-APP-KEY"}, updated_by="u@x.com",
        )
        overlay = await build_overlay(_Base(), Principal(uid="", email="u@x.com"), svc)
        client = create_scm_client(overlay)
        # The credential carries the USER's app id/installation (int-coerced),
        # not the platform's 999/111.
        cred = client._transport.credential
        assert cred._app_id == 424242
        assert cred._installation_id == 77
        assert cred._private_key == "USER-APP-KEY"
        await client.close()

    asyncio.run(go())


# ── resolver: own connector → client; none → None (falls back to platform) ──


def test_resolver_returns_client_for_user_with_connector():
    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await svc.upsert_connector(
            scope=Scope.USER, scope_id="u@x.com", connector_key="scm", provider="github",
            secret_values={"scm_token": "ghp_USERPAT"}, updated_by="u@x.com",
        )
        r = PrincipalSCMResolver(_Base(), svc)
        client = await r.resolve_for_email("u@x.com")
        assert client is not None
        assert await client._get_token() == "ghp_USERPAT"
        # cached: same instance second time
        assert await r.resolve_for_email("u@x.com") is client

    asyncio.run(go())


def test_resolver_none_for_user_without_connector():
    svc = SettingsService(pool=None, secrets=_FakeSecrets())
    r = PrincipalSCMResolver(_Base(), svc)
    assert asyncio.run(r.resolve_for_email("nobody@x.com")) is None
    assert asyncio.run(r.resolve_for_email("not-an-email")) is None


@pytest.mark.asyncio
async def test_scm_for_principal_falls_back_to_platform():
    from devai.pipeline.interfaces import StageDeps

    class _PlatformSCM:
        pass

    platform = _PlatformSCM()
    svc = SettingsService(pool=None, secrets=_FakeSecrets())
    deps = StageDeps(config=_Base(), scm=platform, scm_resolver=PrincipalSCMResolver(_Base(), svc))
    # No connector → platform client; webhook/system → platform client.
    assert await deps.scm_for_principal("nobody@x.com") is platform
    assert await deps.scm_for_principal("webhook:tesserix/x#1") is platform
