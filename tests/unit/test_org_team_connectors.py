"""Org/team connector sharing — isolation, admin guardrails, overlay precedence.

The architecture: a team/org admin shares a connector once; members inherit it
via the overlay; cross-org members never see it; a user can override at user
scope (warned in the UI). These tests prove the isolation + precedence + the
admin write-gate without a live DB (in-memory SettingsService + a fake
TeamService).
"""

from __future__ import annotations

import asyncio

import pytest

from devai.identity import Principal
from devai.settings.models import Scope
from devai.settings.overlay import build_overlay
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
    scm_provider = "github"
    scm_auth_method = "github_app"
    scm_token = ""
    github_app_id = 0
    github_app_private_key = ""
    github_app_installation_id = 0
    github_org = ""


def _scm(svc, scope, scope_id, token, **prefs):
    return svc.upsert_connector(
        scope=scope,
        scope_id=scope_id,
        connector_key="scm",
        provider="github",
        prefs=prefs,
        secret_values={"scm_token": token},
        updated_by="admin@tesserix.app",
    )


# ── Scope.ORG exists in the resolution order ────────────────────────────────


def test_scope_order_includes_org_between_team_and_tenant():
    order = [s.value for s in Scope.order()]
    assert order == ["user", "team", "org", "tenant", "global"]


# ── overlay precedence: user > team > org ───────────────────────────────────


def test_overlay_org_connector_inherited_by_member():
    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await _scm(svc, Scope.ORG, "tesserix", "ORG-PAT")
        # A member of org tesserix (verified via team membership) inherits it.
        p = Principal(uid="u1", email="dev@tesserix.app", team_ids=["team-1"], org_ids=["tesserix"])
        overlay = await build_overlay(_Base(), p, svc)
        assert overlay.scm_token == "ORG-PAT"
        assert overlay.scm_auth_method == "pat"  # inferred

    asyncio.run(go())


def test_overlay_user_overrides_team_overrides_org():
    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await _scm(svc, Scope.ORG, "tesserix", "ORG-PAT")
        await _scm(svc, Scope.TEAM, "team-1", "TEAM-PAT")
        await _scm(svc, Scope.USER, "dev@tesserix.app", "USER-PAT")
        p = Principal(uid="u1", email="dev@tesserix.app", team_ids=["team-1"], org_ids=["tesserix"])
        overlay = await build_overlay(_Base(), p, svc)
        assert overlay.scm_token == "USER-PAT"  # user wins

        # Without a user connector, the team wins over the org.
        p2 = Principal(uid="u2", email="other@tesserix.app", team_ids=["team-1"], org_ids=["tesserix"])
        overlay2 = await build_overlay(_Base(), p2, svc)
        assert overlay2.scm_token == "TEAM-PAT"

    asyncio.run(go())


# ── isolation: a different org never resolves it ────────────────────────────


def test_org_connector_not_visible_cross_org():
    svc = SettingsService(pool=None, secrets=_FakeSecrets())

    async def go():
        await _scm(svc, Scope.ORG, "tesserix", "ORG-PAT")
        # A user in a DIFFERENT org (civica) — not a member of tesserix — gets
        # the base settings, never the tesserix connector.
        outsider = Principal(uid="o1", email="x@civica.com", team_ids=["team-9"], org_ids=["civica"])
        overlay = await build_overlay(_Base(), outsider, svc)
        assert getattr(overlay, "scm_token", "") == ""  # base, not ORG-PAT

    asyncio.run(go())


# ── write authz: team/org admin required ────────────────────────────────────


class _FakeTeams:
    """team_id → {admin_uids}; org_id → {admin_uids}."""

    def __init__(self, team_admins, org_admins):
        self._team_admins = team_admins
        self._org_admins = org_admins

    async def is_team_admin(self, team_id, user_key):
        return user_key in self._team_admins.get(team_id, set())

    async def is_org_admin(self, org_id, user_key):
        return user_key in self._org_admins.get(org_id, set())


class _AppState:
    def __init__(self, team_service):
        self.team_service = team_service


class _Req:
    def __init__(self, team_service):
        self.app = type("A", (), {"state": _AppState(team_service)})()


async def _authz(team_service, principal, scope, scope_id):
    from devai.settings.routes import _authorize

    return await _authorize(_Req(team_service), principal, scope, scope_id)


@pytest.mark.asyncio
async def test_team_write_requires_team_admin():
    from fastapi import HTTPException

    teams = _FakeTeams(team_admins={"team-1": {"u-admin"}}, org_admins={})
    admin = Principal(uid="u-admin", email="a@x.com", team_ids=["team-1"])
    member = Principal(uid="u-member", email="m@x.com", team_ids=["team-1"])

    await _authz(teams, admin, Scope.TEAM, "team-1")  # admin: ok
    with pytest.raises(HTTPException) as e:
        await _authz(teams, member, Scope.TEAM, "team-1")  # plain member: 403
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_org_write_requires_org_admin():
    from fastapi import HTTPException

    teams = _FakeTeams(team_admins={}, org_admins={"tesserix": {"u-admin"}})
    admin = Principal(uid="u-admin", email="a@tesserix.app", org_ids=["tesserix"])
    member = Principal(uid="u-member", email="m@tesserix.app", org_ids=["tesserix"])

    await _authz(teams, admin, Scope.ORG, "tesserix")  # org admin: ok
    with pytest.raises(HTTPException):
        await _authz(teams, member, Scope.ORG, "tesserix")  # member: 403


@pytest.mark.asyncio
async def test_global_admin_overrides_all_scopes():
    teams = _FakeTeams(team_admins={}, org_admins={})
    gadmin = Principal(uid="g", email="g@x.com", roles=["admin"])
    await _authz(teams, gadmin, Scope.TEAM, "any-team")
    await _authz(teams, gadmin, Scope.ORG, "any-org")
    await _authz(teams, gadmin, Scope.TENANT, "alm")


@pytest.mark.asyncio
async def test_user_cannot_write_another_users_scope():
    from fastapi import HTTPException

    teams = _FakeTeams(team_admins={}, org_admins={})
    me = Principal(uid="me", email="me@x.com")
    await _authz(teams, me, Scope.USER, "me")  # own: ok
    with pytest.raises(HTTPException):
        await _authz(teams, me, Scope.USER, "someone-else")
