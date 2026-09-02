"""Identity propagation — extractor + threading into A2A + DevAITask.

These tests don't stand up the full FastAPI app; they exercise the small
seams where identity travels:

  1. Principal serialization roundtrip
  2. extract_principal honors X-Forwarded-* headers before the cookie
  3. extract_principal falls back to the Redis session lookup
  4. A2ABus stamps triggered_by + trace_id on every outbound message
  5. DevAITask serializes principal/trace_id/triggered_by faithfully
  6. _principal_from_webhook builds a coherent Principal from a payload

If any of these regress, the audit trail breaks silently — a triggered
run gets attributed to ``system@devai`` or the literal string ``human``
and the dashboard's "who asked for this?" column goes blank. None of
that is loud at runtime, hence the explicit tests.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from devai.graph.a2a import A2ABus
from devai.identity import (
    SYSTEM_PRINCIPAL_EMAIL,
    Principal,
    extract_principal,
    new_trace_id,
    trace_id_from_request,
)
from devai.pipeline.types import DevAITask
from devai.webhook.routes import _principal_from_webhook

# ──────────────────────────────────────────────────────────────────────
# Principal roundtrip
# ──────────────────────────────────────────────────────────────────────


def test_principal_to_from_dict_roundtrip() -> None:
    p = Principal(
        email="alice@x.com",
        uid="alice-uid",
        tenant_id="tenant-1",
        pool="alm",
        auth_provider="auth-bff",
        roles=["admin", "developer"],
        display_name="Alice",
    )
    back = Principal.from_dict(p.to_dict())
    assert back is not None
    assert back == p


def test_principal_from_dict_handles_none() -> None:
    assert Principal.from_dict(None) is None
    assert Principal.from_dict({}) is None  # empty dict has no email


def test_principal_system_fallback() -> None:
    p = Principal.system()
    assert p.email == SYSTEM_PRINCIPAL_EMAIL
    assert p.auth_provider == "system"


def test_principal_webhook_helper() -> None:
    p = Principal.webhook("github", "octocat", "octocat@github.com")
    assert p.email == "octocat@github.com"
    assert p.uid == "octocat"
    assert p.auth_provider == "webhook:github"
    assert p.display_name == "octocat"


def test_principal_webhook_synthesizes_email_when_missing() -> None:
    p = Principal.webhook("gitlab", "ada-lovelace", "")
    assert p.email == "ada-lovelace@gitlab"


# ──────────────────────────────────────────────────────────────────────
# extract_principal — headers > cookie
# ──────────────────────────────────────────────────────────────────────


def _make_request(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> Any:
    """Build a duck-typed Request that satisfies extract_principal."""
    req = MagicMock()
    req.headers = headers or {}
    req.cookies = cookies or {}
    req.app.state.state_manager = MagicMock()
    # Real Settings default auth_bff_shared_secret to "" (forwarded headers
    # trusted unconditionally). A bare MagicMock would auto-create a truthy
    # attribute, making _forward_trusted reject the unsigned forwarded headers.
    req.app.state.config.auth_bff_shared_secret = ""
    return req


@pytest.mark.asyncio
async def test_extract_principal_prefers_auth_bff_headers() -> None:
    req = _make_request(
        headers={
            "x-forwarded-user": "alice@x.com",
            "x-forwarded-uid": "alice-uid",
            "x-forwarded-tenant": "tenant-1",
            "x-forwarded-name": "Alice",
            "x-forwarded-roles": "developer,platform-admin",
        }
    )
    principal = await extract_principal(req)
    assert principal is not None
    assert principal.email == "alice@x.com"
    assert principal.uid == "alice-uid"
    assert principal.tenant_id == "tenant-1"
    assert principal.auth_provider == "auth-bff"
    assert principal.display_name == "Alice"
    assert principal.roles == ["developer", "platform-admin"]


@pytest.mark.asyncio
async def test_extract_principal_falls_back_to_session_cookie() -> None:
    req = _make_request(cookies={"devai_session": "sess-abc"})
    session_payload = json.dumps(
        {
            "user_email": "bob@x.com",
            "user_login": "bob",
            "user_name": "Bob",
            "auth_provider": "keycloak",
            "roles": ["dev"],
        }
    )
    req.app.state.state_manager.redis.get = AsyncMock(return_value=session_payload)

    principal = await extract_principal(req)
    assert principal is not None
    assert principal.email == "bob@x.com"
    assert principal.auth_provider == "keycloak"
    assert principal.roles == ["dev"]


@pytest.mark.asyncio
async def test_extract_principal_returns_none_when_nothing_present() -> None:
    req = _make_request()
    req.app.state.state_manager.redis.get = AsyncMock(return_value=None)
    assert await extract_principal(req) is None


# ──────────────────────────────────────────────────────────────────────
# Trace id resolution
# ──────────────────────────────────────────────────────────────────────


def test_trace_id_honors_traceparent() -> None:
    req = _make_request(headers={"traceparent": "00-1234567890abcdef1234567890abcdef-0000000000000001-01"})
    tid = trace_id_from_request(req)
    assert tid == "1234567890abcdef1234567890abcdef"


def test_trace_id_honors_x_request_id_when_no_traceparent() -> None:
    req = _make_request(headers={"x-request-id": "req-xyz"})
    assert trace_id_from_request(req) == "req-xyz"


def test_trace_id_mints_when_nothing_provided() -> None:
    req = _make_request()
    tid = trace_id_from_request(req)
    # 32 hex chars (UUID4 hex)
    assert len(tid) == 32
    int(tid, 16)  # raises if not hex


def test_new_trace_id_is_unique_per_call() -> None:
    assert new_trace_id() != new_trace_id()


# ──────────────────────────────────────────────────────────────────────
# A2A bus stamps identity on every outbound message
# ──────────────────────────────────────────────────────────────────────


def test_a2a_bus_stamps_triggered_by_and_trace_id() -> None:
    bus = A2ABus("senior_developer", triggered_by="alice@x.com", trace_id="trace-abc")
    msg = bus.send("staff_reviewer", "handoff", "implement done", "PR #42")
    assert msg["triggered_by"] == "alice@x.com"
    assert msg["trace_id"] == "trace-abc"
    assert msg["from_agent"] == "senior_developer"
    assert msg["to_agent"] == "staff_reviewer"
    assert msg["message_type"] == "handoff"


def test_a2a_bus_omits_identity_when_not_set() -> None:
    bus = A2ABus("anon_agent")
    msg = bus.send("peer", "notification", "x", "y")
    assert "triggered_by" not in msg
    assert "trace_id" not in msg


def test_a2a_bus_handoff_inherits_identity() -> None:
    bus = A2ABus("engineering_manager", triggered_by="alice@x.com", trace_id="t-1")
    handoff = bus.handoff("senior_developer", "build story 3", "details…")
    assert handoff["triggered_by"] == "alice@x.com"
    assert handoff["trace_id"] == "t-1"
    assert handoff["message_type"] == "handoff"


# ──────────────────────────────────────────────────────────────────────
# DevAITask carries identity through serialization
# ──────────────────────────────────────────────────────────────────────


def test_devai_task_serializes_identity_fields() -> None:
    task = DevAITask(
        intent="add pagination",
        repo="tesserix/demo",
        principal={"email": "alice@x.com", "uid": "u-1", "auth_provider": "auth-bff"},
        trace_id="trace-xyz",
        triggered_by="alice@x.com",
    )
    out = task.to_dict()
    assert out["triggered_by"] == "alice@x.com"
    assert out["trace_id"] == "trace-xyz"
    assert out["principal"] == {"email": "alice@x.com", "uid": "u-1", "auth_provider": "auth-bff"}


def test_devai_task_identity_defaults_to_none() -> None:
    task = DevAITask(intent="x")
    out = task.to_dict()
    assert out["triggered_by"] is None
    assert out["trace_id"] is None
    assert out["principal"] is None


# ──────────────────────────────────────────────────────────────────────
# Webhook-payload → Principal helper
# ──────────────────────────────────────────────────────────────────────


def test_principal_from_webhook_github_payload() -> None:
    payload = {"sender": {"login": "octocat", "email": "octocat@github.com"}}
    p = _principal_from_webhook("github", payload)
    assert p.email == "octocat@github.com"
    assert p.uid == "octocat"
    assert p.auth_provider == "webhook:github"


def test_principal_from_webhook_gitlab_user_block() -> None:
    payload = {"user": {"username": "ada", "email": ""}}
    p = _principal_from_webhook("gitlab", payload)
    assert p.email == "ada@gitlab"
    assert p.uid == "ada"


def test_principal_from_webhook_unknown_sender_falls_back() -> None:
    p = _principal_from_webhook("ado", {})
    assert p.email == "unknown@ado"


# ── MCP Hub service bearer ─────────────────────────────────────────────


def _make_bearer_request(token_header: str, configured: str) -> Any:
    req = _make_request(headers={"authorization": token_header} if token_header else {})
    req.app.state.config.mcp_hub_service_token = configured
    return req


@pytest.mark.asyncio
async def test_service_bearer_resolves_hub_principal() -> None:
    token = "a-strong-shared-service-token-123"
    req = _make_bearer_request(f"Bearer {token}", token)
    principal = await extract_principal(req)
    assert principal is not None
    assert principal.uid == "svc-mcp-hub"
    assert principal.auth_provider == "service-token"
    assert "service" in principal.roles


@pytest.mark.asyncio
async def test_service_bearer_wrong_token_rejected() -> None:
    req = _make_bearer_request("Bearer not-the-right-token-at-all", "a-strong-shared-service-token-123")
    principal = await extract_principal(req)
    assert principal is None


@pytest.mark.asyncio
async def test_service_bearer_short_configured_token_never_matches() -> None:
    # A trivially short configured token must not enable the path at all.
    req = _make_bearer_request("Bearer short", "short")
    principal = await extract_principal(req)
    assert principal is None


def test_user_scope_id_is_tenant_qualified() -> None:
    tenant_a = Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-a")
    tenant_b = Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-b")

    assert tenant_a.user_scope_id == "tenant-a:shared-uid"
    assert tenant_b.user_scope_id == "tenant-b:shared-uid"
    assert tenant_a.user_scope_id != tenant_b.user_scope_id


def test_user_scope_id_keeps_legacy_shape_without_tenant() -> None:
    principal = Principal(email="local@example.com", uid="local-uid")

    assert principal.user_scope_id == "local-uid"
