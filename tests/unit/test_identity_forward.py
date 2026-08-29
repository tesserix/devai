"""Tests for the auth-bff shared-secret gate on X-Forwarded-* identity."""

from types import SimpleNamespace

from devai.identity import _forward_trusted, extract_principal


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeRequest:
    def __init__(self, headers, secret="", cookies=None):
        self.headers = _Headers(headers)
        self.cookies = cookies or {}
        self.app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(auth_bff_shared_secret=secret)))


def test_forward_trusted_when_no_secret_configured():
    req = _FakeRequest({"x-forwarded-user": "a@b.com"}, secret="")
    assert _forward_trusted(req)


def test_forward_trusted_with_matching_secret():
    req = _FakeRequest({"x-forwarded-user": "a@b.com", "x-auth-bff-secret": "s3cret"}, secret="s3cret")
    assert _forward_trusted(req)


def test_forward_trusted_when_configured_secret_has_trailing_newline():
    req = _FakeRequest(
        {"x-forwarded-user": "a@b.com", "x-auth-bff-secret": "s3cret"},
        secret="s3cret\n",
    )
    assert _forward_trusted(req)


def test_forward_not_trusted_without_secret_header():
    req = _FakeRequest({"x-forwarded-user": "a@b.com"}, secret="s3cret")
    assert not _forward_trusted(req)


def test_forward_not_trusted_with_wrong_secret():
    req = _FakeRequest({"x-forwarded-user": "a@b.com", "x-auth-bff-secret": "nope"}, secret="s3cret")
    assert not _forward_trusted(req)


async def test_extract_principal_honors_secret_gate():
    # Configured secret, no secret header → forwarded identity ignored,
    # no cookie → no principal.
    spoof = _FakeRequest({"x-forwarded-user": "attacker@evil.com"}, secret="s3cret")
    assert await extract_principal(spoof) is None

    # Correct secret → principal resolves from the forwarded header.
    legit = _FakeRequest({"x-forwarded-user": "alice@corp.com", "x-auth-bff-secret": "s3cret"}, secret="s3cret")
    p = await extract_principal(legit)
    assert p is not None and p.email == "alice@corp.com"
