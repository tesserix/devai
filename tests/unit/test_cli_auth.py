from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from devai.cli import adk_commands
from devai.cli.auth_commands import (
    CLIAuthError,
    KeyringSessionStore,
    LoopbackCallback,
    SignInProof,
    StoredSession,
    exchange_proof,
)
from devai.cli.commands import app


class MemoryKeyring:
    def __init__(self) -> None:
        self.payload: str | None = None

    def get_password(self, service: str, username: str) -> str | None:
        assert (service, username) == ("devai-cli", "active-session")
        return self.payload

    def set_password(self, service: str, username: str, password: str) -> None:
        assert (service, username) == ("devai-cli", "active-session")
        self.payload = password

    def delete_password(self, service: str, username: str) -> None:
        assert (service, username) == ("devai-cli", "active-session")
        self.payload = None


def test_loopback_callback_requires_exact_origin_state_and_supports_private_network_preflight() -> None:
    state = "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s"
    callback = LoopbackCallback(state=state, expected_origin="https://devai.tesserix.app")

    preflight = callback.handle(
        method="OPTIONS",
        path="/callback",
        headers={
            "origin": "https://devai.tesserix.app",
            "access-control-request-method": "POST",
            "access-control-request-private-network": "true",
        },
        body=b"",
    )
    rejected = callback.handle(
        method="POST",
        path="/callback",
        headers={"origin": "https://attacker.example", "content-type": "application/json"},
        body=json.dumps({"id_token": "stolen", "pool": "alm", "tenant_id": "tenant-alm", "state": state}).encode(),
    )
    accepted = callback.handle(
        method="POST",
        path="/callback",
        headers={"origin": "https://devai.tesserix.app", "content-type": "application/json"},
        body=json.dumps({"id_token": "gip-proof", "pool": "alm", "tenant_id": "tenant-alm", "state": state}).encode(),
    )

    assert preflight.status == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "https://devai.tesserix.app"
    assert preflight.headers["Access-Control-Allow-Private-Network"] == "true"
    assert rejected.status == 403
    assert rejected.proof is None
    assert callback.proof is not None
    assert callback.proof.id_token.get_secret_value() == "gip-proof"
    assert accepted.status == 204
    assert accepted.proof == callback.proof


@pytest.mark.parametrize(
    ("path", "content_type", "state"),
    [
        ("/other", "application/json", "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s"),
        ("/callback", "text/plain", "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s"),
        ("/callback", "application/json", "wrong-state"),
    ],
    ids=["wrong-path", "wrong-content-type", "wrong-state"],
)
def test_loopback_callback_rejects_malformed_handoffs(path: str, content_type: str, state: str) -> None:
    callback = LoopbackCallback(
        state="R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s",
        expected_origin="https://devai.tesserix.app",
    )

    response = callback.handle(
        method="POST",
        path=path,
        headers={"origin": "https://devai.tesserix.app", "content-type": content_type},
        body=json.dumps({"id_token": "gip-proof", "pool": "alm", "tenant_id": "tenant-alm", "state": state}).encode(),
    )

    assert response.status in {400, 404, 415}
    assert callback.proof is None


def test_keyring_store_round_trips_and_expires_without_disk_storage() -> None:
    backend = MemoryKeyring()
    store = KeyringSessionStore(backend=backend)
    now = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)
    session = StoredSession(
        base_url="https://devai.tesserix.app:443/",
        cookie=SecretStr("encrypted-session"),
        email="user@example.com",
        expires_at=now + timedelta(hours=1),
    )

    store.save(session)

    assert session.base_url == "https://devai.tesserix.app"
    assert backend.payload is not None
    assert "encrypted-session" in backend.payload
    assert store.load(now=now) == session
    assert store.load(now=now + timedelta(hours=2)) is None
    assert backend.payload is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "base_url": "https://devai.tesserix.app/path",
            "cookie": "encrypted-session",
            "email": "user@example.com",
            "expires_at": "2026-08-29T05:00:00+00:00",
        },
        {
            "base_url": "https://devai.tesserix.app",
            "cookie": "encrypted-session",
            "email": "user@example.com",
            "expires_at": "2026-08-29T05:00:00",
        },
    ],
    ids=["non-origin-url", "naive-expiry"],
)
def test_keyring_store_removes_invalid_session_records(payload: dict[str, str]) -> None:
    backend = MemoryKeyring()
    backend.payload = json.dumps(payload)
    store = KeyringSessionStore(backend=backend)

    with pytest.raises(CLIAuthError):
        store.load(now=datetime(2026, 8, 29, 3, 0, tzinfo=UTC))

    assert backend.payload is None


def test_exchange_proof_verifies_session_and_never_persists_identity_proof() -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/auto-login":
            payload: dict[str, Any] = json.loads(request.content)
            assert payload == {
                "id_token": "gip-proof",
                "expected_tenant_id": "tenant-alm",
                "pool": "alm",
                "client_type": "cli",
            }
            return httpx.Response(
                200,
                headers={"set-cookie": "devai_session=encrypted-session; Path=/; Secure; HttpOnly; SameSite=Lax"},
                json={"email": "user@example.com"},
            )
        assert request.url.path == "/auth/me"
        assert request.headers["cookie"] == "devai_session=encrypted-session"
        return httpx.Response(200, json={"email": "user@example.com", "exp": expires_at.isoformat()})

    session = exchange_proof(
        base_url="https://devai.tesserix.app",
        proof=SignInProof(id_token=SecretStr("gip-proof"), pool="alm", tenant_id="tenant-alm"),
        transport=httpx.MockTransport(handle),
    )

    assert session.cookie.get_secret_value() == "encrypted-session"
    assert session.email == "user@example.com"
    assert session.expires_at == expires_at
    assert len(requests) == 2


def test_exchange_proof_redacts_server_errors() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(401, json={"message": "rejected Authorization: Bearer gip-secret"})
    )

    with pytest.raises(CLIAuthError) as caught:
        exchange_proof(
            base_url="https://devai.tesserix.app",
            proof=SignInProof(id_token=SecretStr("gip-proof"), pool="alm", tenant_id="tenant-alm"),
            transport=transport,
        )

    assert "gip-secret" not in str(caught.value)


def test_top_level_cli_registers_auth_commands() -> None:
    result = CliRunner().invoke(app, ["auth", "--help"])

    assert result.exit_code == 0, result.output
    assert "login" in result.output
    assert "status" in result.output
    assert "logout" in result.output


def test_adk_client_automatically_uses_origin_bound_keyring_session(monkeypatch: pytest.MonkeyPatch) -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    stored = StoredSession(
        base_url="https://devai.tesserix.app",
        cookie=SecretStr("encrypted-session"),
        email="user@example.com",
        expires_at=expires_at,
    )
    captured: dict[str, str] = {}

    class CapturingClient:
        def __init__(self, *, base_url: str, session_cookie: str, token: str) -> None:
            captured.update(base_url=base_url, session_cookie=session_cookie, token=token)

    monkeypatch.delenv("DEVAI_API_URL", raising=False)
    monkeypatch.delenv("DEVAI_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("DEVAI_API_TOKEN", raising=False)
    monkeypatch.setattr(adk_commands, "load_stored_session", lambda: stored)
    monkeypatch.setattr(adk_commands, "SandboxClient", CapturingClient)

    adk_commands._new_sandbox_client()

    assert captured == {
        "base_url": "https://devai.tesserix.app",
        "session_cookie": "encrypted-session",
        "token": "",
    }


def test_adk_client_never_sends_stored_session_to_another_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = StoredSession(
        base_url="https://devai.tesserix.app",
        cookie=SecretStr("encrypted-session"),
        email="user@example.com",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    captured: dict[str, str] = {}

    class CapturingClient:
        def __init__(self, *, base_url: str, session_cookie: str, token: str) -> None:
            captured.update(base_url=base_url, session_cookie=session_cookie, token=token)

    monkeypatch.delenv("DEVAI_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("DEVAI_API_TOKEN", raising=False)
    monkeypatch.setattr(adk_commands, "load_stored_session", lambda: stored)
    monkeypatch.setattr(adk_commands, "SandboxClient", CapturingClient)

    adk_commands._new_sandbox_client(api_url="https://staging-devai.tesserix.app")

    assert captured["base_url"] == "https://staging-devai.tesserix.app"
    assert captured["session_cookie"] == ""
