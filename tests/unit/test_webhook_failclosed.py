"""Regression test: webhooks fail closed under the require_auth posture.

Webhooks are exempt from the DEVAI_REQUIRE_AUTH middleware gate because they
authenticate via HMAC signature. So when require_auth is on but no webhook
secret is configured, the handler must reject (401) rather than accept unsigned
requests. In dev (require_auth off) the lenient behavior is preserved.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.webhook.routes as routes


class _Cfg:
    def __init__(self, *, secret="", require_auth=False):
        self.github_webhook_secret = secret
        self.require_auth = require_auth
        self.pipeline_label = "devai:automate"


class _FakeSCM:
    def __init__(self, valid=True):
        self._valid = valid

    def verify_webhook_signature(self, body, sig, secret):  # noqa: ARG002
        return self._valid

    def parse_webhook_event(self, event_type, payload):  # noqa: ARG002
        return None  # ignored → no dispatch

    async def close(self):
        return None


@pytest.fixture()
def client_factory(monkeypatch):
    def make(cfg, valid=True):
        app = FastAPI()
        app.include_router(routes.router)
        app.state.config = cfg
        import devai.scm as scm_mod

        monkeypatch.setattr(scm_mod, "create_scm_client", lambda c: _FakeSCM(valid))
        return TestClient(app)

    return make


def _post(client):
    return client.post("/webhook/scm", headers={"X-GitHub-Event": "issues"}, content=b"{}")


def test_require_auth_no_secret_rejects(client_factory):
    client = client_factory(_Cfg(secret="", require_auth=True))
    assert _post(client).status_code == 401


def test_dev_no_secret_allows(client_factory):
    client = client_factory(_Cfg(secret="", require_auth=False))
    # No secret + dev posture → lenient (accepted, then ignored by parse).
    assert _post(client).status_code == 200


def test_secret_set_invalid_signature_rejects(client_factory):
    client = client_factory(_Cfg(secret="s", require_auth=True), valid=False)
    assert _post(client).status_code == 401


def test_secret_set_valid_signature_accepts(client_factory):
    client = client_factory(_Cfg(secret="s", require_auth=True), valid=True)
    assert _post(client).status_code == 200
