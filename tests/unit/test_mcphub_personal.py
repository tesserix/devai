"""Per-user MCP federation — a caller's own servers, isolated per principal.

Fakes the downstream MCP connection and the user-connector resolver so the
test needs neither the MCP SDK nor a live settings DB. Proves: a user's
servers appear namespaced ``usr-<instance>__<tool>``; calls route to the
right leg; another user sees nothing; the SSRF guard drops private endpoints.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.mcphub import personal as personal_mod
from devai.mcphub.personal import PersonalLegs, check_external_endpoint

# ── SSRF guard ─────────────────────────────────────────────────────────────


def test_guard_blocks_metadata_and_loopback():
    for bad in ("http://169.254.169.254/mcp", "http://127.0.0.1/mcp", "http://localhost/mcp"):
        with pytest.raises(ValueError):
            check_external_endpoint(bad)


def test_guard_blocks_non_http_scheme():
    with pytest.raises(ValueError):
        check_external_endpoint("ftp://example.com/mcp")


def test_guard_allows_literal_public_ip():
    # A public IP literal skips DNS and passes (1.1.1.1 is routable).
    check_external_endpoint("https://1.1.1.1/mcp")


# ── federation ─────────────────────────────────────────────────────────────


class _FakeConn:
    """Stand-in for DownstreamConnection — no MCP SDK."""

    def __init__(self, spec, headers=None, timeout=30.0):
        self.spec = spec
        self.headers = headers or {}
        self.closed = False

    async def connect(self):
        from devai.mcphub.model import HEALTH_READY

        self.spec.health = HEALTH_READY

    async def list_tools(self):
        return [{"name": "do_thing", "description": "does a thing", "inputSchema": {"type": "object"}}]

    async def call_tool(self, wire, args):
        return SimpleNamespace(content=[{"echo": wire, "args": args, "auth": self.headers}])

    async def close(self):
        self.closed = True


@pytest.fixture
def legs(monkeypatch):
    # No real network: skip the SSRF DNS check and use the fake connection.
    monkeypatch.setattr(personal_mod, "check_external_endpoint", lambda url: None)
    monkeypatch.setattr(personal_mod, "DownstreamConnection", _FakeConn)

    connectors = {
        "alice@x.com": [
            {
                "instance_id": "github",
                "provider": "streamable_http",
                "mcp_url": "https://api.acme.dev/mcp",
                "mcp_token": "tok-alice",
                "mcp_auth_header": "x-api-key",
            }
        ],
        "bob@y.com": [],
    }

    async def fake_resolve(self, email):  # noqa: ANN001
        return connectors.get(email, [])

    monkeypatch.setattr(PersonalLegs, "_resolve_connectors", fake_resolve)
    return PersonalLegs(connect_timeout=1.0, ttl=60.0)


async def test_user_tools_are_namespaced(legs):
    tools = await legs.tool_descriptors("alice@x.com")
    assert [t["name"] for t in tools] == ["usr-github__do_thing"]


async def test_call_routes_with_user_auth(legs):
    await legs.tool_descriptors("alice@x.com")  # warm
    res = await legs.call("alice@x.com", "usr-github__do_thing", {"q": 1})
    assert res.content[0]["echo"] == "do_thing"
    # The user's own token went out on their chosen header.
    assert res.content[0]["auth"]["x-api-key"] == "tok-alice"


async def test_isolation_other_user_sees_nothing(legs):
    from devai.mcphub.downstream import DownstreamError

    assert await legs.tool_descriptors("bob@y.com") == []
    with pytest.raises(DownstreamError):
        await legs.call("bob@y.com", "usr-github__do_thing", {})


async def test_owns_recognizes_personal_names(legs):
    assert legs.owns("usr-github__do_thing") is True
    assert legs.owns("gitops-mcp__argocd_sync") is False


async def test_unknown_email_no_tools(legs):
    assert await legs.tool_descriptors("not-an-email") == []


# ── hub integration ────────────────────────────────────────────────────────


async def test_hub_merges_personal_into_aggregate(monkeypatch):
    """list_tools_for appends the caller's personal tools to the shared surface."""
    from devai.mcphub.hub import MCPHub
    from devai.mcphub.model import FederatedTool

    class _Reg:
        def list_mcp_servers(self):
            return []

        def list_tool_artifacts(self):
            return []

    hub = MCPHub(_Reg())
    # Pretend the shared aggregate already has one registry tool.
    hub._tools = {
        "gitops-mcp__argocd_sync": FederatedTool(
            name="gitops-mcp__argocd_sync", server="gitops-mcp", wire_name="argocd_sync", tier="core"
        )
    }

    async def fake_descriptors(email):
        return (
            [{"name": "usr-gh__do_thing", "description": "d", "input_schema": {"type": "object"}}]
            if email == "alice@x.com"
            else []
        )

    monkeypatch.setattr(hub.personal, "tool_descriptors", fake_descriptors)

    shared = await hub.list_tools_for("")
    assert {t.name for t in shared.selected} == {"gitops-mcp__argocd_sync"}

    withpersonal = await hub.list_tools_for("alice@x.com")
    names = {t.name for t in withpersonal.selected}
    assert names == {"gitops-mcp__argocd_sync", "usr-gh__do_thing"}
