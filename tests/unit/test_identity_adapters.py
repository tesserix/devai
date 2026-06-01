"""Contract tests for the identity adapter family.

Proves the swap is real: prod selects Noop (auth terminated upstream),
local_db actually checks passwords, and a valid result maps to the exact
Redis session shape the dashboard writes / extract_principal reads.
"""

from __future__ import annotations

import pytest

from devai.adapters.identity import create_identity_adapter
from devai.adapters.identity.local_db import LocalDBIdentityAdapter
from devai.adapters.identity.noop import NoopIdentityAdapter

USERS = [
    {"username": "admin", "password": "secret", "email": "a@devai.local", "roles": ["admin"]},
    {"username": "qa", "password": "secret", "roles": ["qa"]},
]


class _S:
    def __init__(self, provider, users=None):
        self.auth_provider = provider
        self.local_auth_users = users or []


def test_factory_prod_is_noop():
    a = create_identity_adapter(_S("keycloak"))
    assert isinstance(a, NoopIdentityAdapter)
    assert a.login_config()["mode"] == "gip"


def test_factory_local_db():
    a = create_identity_adapter(_S("local_db", USERS))
    assert isinstance(a, LocalDBIdentityAdapter)
    cfg = a.login_config()
    assert cfg["mode"] == "local_db"
    assert cfg["usernames"] == ["admin", "qa"]


@pytest.mark.asyncio
async def test_noop_never_authenticates():
    a = NoopIdentityAdapter()
    assert await a.authenticate("admin", "secret") is None


@pytest.mark.asyncio
async def test_local_db_authenticates_and_maps_session():
    a = LocalDBIdentityAdapter(users=USERS)
    ok = await a.authenticate("admin", "secret")
    assert ok is not None
    sess = ok.to_session("local_db")
    # exact keys extract_principal reads
    assert sess["auth_provider"] == "local_db"
    assert sess["user_login"] == "admin"
    assert sess["user_email"] == "a@devai.local"
    assert sess["roles"] == ["admin"]


@pytest.mark.asyncio
async def test_local_db_rejects_bad_password_and_unknown_user():
    a = LocalDBIdentityAdapter(users=USERS)
    assert await a.authenticate("admin", "wrong") is None
    assert await a.authenticate("ghost", "secret") is None


@pytest.mark.asyncio
async def test_local_db_empty_roster_fails_closed():
    a = LocalDBIdentityAdapter(users=[])
    assert await a.authenticate("admin", "secret") is None


@pytest.mark.asyncio
async def test_local_db_synthesizes_email_when_missing():
    a = LocalDBIdentityAdapter(users=USERS)
    ok = await a.authenticate("qa", "secret")
    assert ok is not None and ok.email == "qa@devai.local"
