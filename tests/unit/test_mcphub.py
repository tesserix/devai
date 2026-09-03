"""Unit tests for the DevAI MCP Hub (docs/agentic/MCP-HUB.md §6).

Covers the pure contract (naming/routing, discovery, auth injection, profile
budgeting) plus the hub's aggregation/routing/degradation with a fake downstream
— so the whole multiplexer is verified without the ``mcp`` SDK or a live
registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.mcphub.discovery import discover, downstream_headers, spec_from_record
from devai.mcphub.model import (
    DownstreamSpec,
    FederatedTool,
    RouteError,
    namespaced,
    namespaced_uri,
    route,
    route_uri,
)
from devai.mcphub.profile import DEFAULT_MAX_TOOLS, ToolProfile, select
from devai.registry.client import McpServer

# --------------------------------------------------------------------------- #
# Naming / routing — collision-free, reversible
# --------------------------------------------------------------------------- #


def test_namespaced_round_trips():
    name = namespaced("analyst-mcp", "security_scan_sast")
    assert name == "analyst-mcp__security_scan_sast"
    assert route(name) == ("analyst-mcp", "security_scan_sast")


def test_wire_name_with_double_underscore_round_trips():
    # route() splits on the FIRST separator, so a wire name with `__` survives.
    name = namespaced("scm-mcp", "create__pr")
    assert route(name) == ("scm-mcp", "create__pr")


def test_namespacing_is_collision_free():
    a = namespaced("analyst-mcp", "deploy")
    b = namespaced("sre-mcp", "deploy")
    assert a != b
    assert route(a)[0] == "analyst-mcp"
    assert route(b)[0] == "sre-mcp"


def test_route_rejects_non_namespaced():
    with pytest.raises(RouteError):
        route("not_namespaced")
    with pytest.raises(RouteError):
        namespaced("bad__server", "x")  # server may not contain the separator


def test_resource_uri_round_trips():
    u = namespaced_uri("analyst-mcp", "file:///report.json")
    assert u == "mcp+analyst-mcp+file:///report.json"
    assert route_uri(u) == ("analyst-mcp", "file:///report.json")
    with pytest.raises(RouteError):
        route_uri("file:///plain")


# --------------------------------------------------------------------------- #
# Discovery + auth injection
# --------------------------------------------------------------------------- #


def _server(name, **raw):
    raw.setdefault("transport", "streamable-http")
    return McpServer(name=name, type=raw.get("transport", ""), url=raw.get("endpoint", ""), raw=raw)


def test_spec_from_record_maps_fields():
    rec = _server(
        "analyst-mcp",
        endpoint="http://devai-api:8080/mcp/analyst",
        transport="streamable-http",
        authMode="jwt",
        metadata={"labels": {"devai.io/category": "analyst"}},
    )
    spec = spec_from_record(rec)
    assert spec.name == "analyst-mcp"
    assert spec.endpoint == "http://devai-api:8080/mcp/analyst"
    assert spec.auth_mode == "jwt"
    assert spec.labels["devai.io/category"] == "analyst"
    assert spec.is_servable()


def test_discover_skips_non_servable():
    class FakeReg:
        def list_mcp_servers(self):
            return [
                _server("ok-mcp", endpoint="http://x/mcp"),
                _server("stdio-mcp", transport="stdio", command="run"),  # not dialable
                _server("noendpoint-mcp"),  # no endpoint
            ]

    specs = discover(FakeReg())
    assert [s.name for s in specs] == ["ok-mcp"]


def test_discover_degrades_on_registry_error():
    class BoomReg:
        def list_mcp_servers(self):
            raise RuntimeError("registry down")

    assert discover(BoomReg()) == []


def test_downstream_headers_per_auth_mode():
    none_spec = DownstreamSpec("a", "http://a", auth_mode="none")
    assert downstream_headers(none_spec) == {}

    hdr_spec = DownstreamSpec("b", "http://b", auth_mode="header", headers={"X-Key": "k"})
    assert downstream_headers(hdr_spec) == {"X-Key": "k"}

    jwt_spec = DownstreamSpec("c", "http://c", auth_mode="jwt")
    assert downstream_headers(jwt_spec, service_token="tok")["Authorization"] == "Bearer tok"
    # jwt with no token → no bearer (degrade, don't crash)
    assert "Authorization" not in downstream_headers(jwt_spec)

    mtls_spec = DownstreamSpec("d", "http://d", auth_mode="mtls", headers={"X": "1"})
    assert downstream_headers(mtls_spec) == {"X": "1"}  # cert is transport-level


# --------------------------------------------------------------------------- #
# Profile budgeting
# --------------------------------------------------------------------------- #


def _tools(*specs):
    return [FederatedTool.build(srv, wire, tier=tier) for srv, wire, tier in specs]


def test_default_profile_keeps_only_core_tier():
    tools = _tools(("a", "t1", "core"), ("a", "t2", "extended"), ("b", "t3", "experimental"))
    res = select(tools, ToolProfile.default())
    assert [t.wire_name for t in res.selected] == ["t1"]
    assert set(res.dropped_by_filter) == {"a__t2", "b__t3"}


def test_allow_pin_bypasses_tier():
    tools = _tools(("a", "t1", "core"), ("a", "t2", "experimental"))
    prof = ToolProfile(name="pinned", allow=frozenset({"a__t2"}))
    res = select(tools, prof)
    assert [t.name for t in res.selected] == ["a__t2"]


def test_server_restriction():
    tools = _tools(("a", "t1", "core"), ("b", "t2", "core"))
    prof = ToolProfile(name="aonly", servers=frozenset({"a"}))
    res = select(tools, prof)
    assert [t.server for t in res.selected] == ["a"]


def test_budget_cap_truncates_and_reports():
    tools = _tools(*[("a", f"t{i:02d}", "core") for i in range(50)])
    prof = ToolProfile(name="capped", max_tools=10)
    res = select(tools, prof)
    assert len(res.selected) == 10
    assert len(res.dropped_by_budget) == 40
    assert res.truncated
    # deterministic: first 10 by sorted namespaced name
    assert [t.wire_name for t in res.selected] == [f"t{i:02d}" for i in range(10)]


def test_default_budget_is_documented_ceiling():
    assert ToolProfile.default().max_tools == DEFAULT_MAX_TOOLS == 40


# --------------------------------------------------------------------------- #
# Hub aggregation / routing / degradation (fake downstream, no SDK)
# --------------------------------------------------------------------------- #

_CANNED = {
    "analyst-mcp": [
        {"name": "security_scan_sast", "description": "SAST", "inputSchema": {"type": "object"}},
        {"name": "validate_compile", "description": "compile", "inputSchema": {}},
    ],
    "sre-mcp": [
        {"name": "list_pods", "description": "pods", "inputSchema": {}},
    ],
}


class _FakeConn:
    """Stand-in for DownstreamConnection — no MCP SDK, canned responses."""

    def __init__(self, spec, headers=None, timeout=30.0):
        self.spec = spec

    @property
    def name(self):
        return self.spec.name

    @property
    def healthy(self):
        from devai.mcphub.model import HEALTH_READY

        return self.spec.health == HEALTH_READY

    async def connect(self):
        from devai.mcphub.downstream import DownstreamError
        from devai.mcphub.model import HEALTH_READY, HEALTH_UNREACHABLE

        if self.spec.name == "broken-mcp":
            self.spec.health = HEALTH_UNREACHABLE
            raise DownstreamError("boom")
        self.spec.health = HEALTH_READY

    async def list_tools(self):
        return list(_CANNED.get(self.spec.name, []))

    async def list_prompts(self):
        return []

    async def list_resources(self):
        return []

    async def call_tool(self, wire, args):
        return SimpleNamespace(content=[{"echo": wire, "args": args}])

    async def close(self):
        return None


class _FakeRegistry:
    def list_mcp_servers(self):
        return [
            _server("analyst-mcp", endpoint="http://a/mcp"),
            _server("sre-mcp", endpoint="http://s/mcp"),
            _server("broken-mcp", endpoint="http://b/mcp"),
        ]

    def list_tool_artifacts(self):
        return [
            {
                "metadata": {
                    "name": "analyst-security-scan-sast",
                    "labels": {"mcp.devai.io/server": "analyst-mcp", "devai.io/tier": "core"},
                    "annotations": {"mcp.devai.io/wire-name": "security_scan_sast"},
                },
                "spec": {"description": "SAST scan"},
            },
            {
                "metadata": {
                    "name": "analyst-validate-compile",
                    "labels": {"mcp.devai.io/server": "analyst-mcp", "devai.io/tier": "extended"},
                    "annotations": {"mcp.devai.io/wire-name": "validate_compile"},
                },
                "spec": {"description": "compile check"},
            },
        ]


@pytest.fixture
def hub(monkeypatch):
    from devai.mcphub import hub as hub_mod

    monkeypatch.setattr(hub_mod, "DownstreamConnection", _FakeConn)
    return hub_mod.MCPHub(_FakeRegistry())


async def test_hub_refresh_aggregates_and_namespaces(hub):
    await hub.refresh()
    names = {t.name for t in hub.list_tools(ToolProfile.unrestricted()).selected}
    assert names == {
        "analyst-mcp__security_scan_sast",
        "analyst-mcp__validate_compile",
        "sre-mcp__list_pods",
    }


async def test_hub_drops_unreachable_leg(hub):
    await hub.refresh()
    status = hub.status()
    served = {d["name"] for d in status["downstreams"]}
    # broken-mcp failed connect → never added to the connection table.
    assert "broken-mcp" not in served
    assert {"analyst-mcp", "sre-mcp"} <= served


async def test_hub_enriches_tier_from_registry(hub):
    await hub.refresh()
    # Default profile = core only → validate_compile (extended) is filtered out.
    core = {t.name for t in hub.list_tools(ToolProfile.default()).selected}
    assert "analyst-mcp__security_scan_sast" in core
    assert "analyst-mcp__validate_compile" not in core


async def test_hub_routes_tool_call_to_downstream(hub):
    await hub.refresh()
    result = await hub.call_tool("sre-mcp__list_pods", {"ns": "devai"})
    assert result.content == [{"echo": "list_pods", "args": {"ns": "devai"}}]


async def test_hub_call_to_unknown_leg_errors_cleanly(hub):
    from devai.mcphub.downstream import DownstreamError

    await hub.refresh()
    with pytest.raises(DownstreamError):
        await hub.call_tool("broken-mcp__whatever", {})


async def test_hub_refresh_needs_no_reverse_session_callback(hub):
    await hub.refresh()
    assert not hasattr(hub, "on_changed")


# --------------------------------------------------------------------------- #
# Phase 6 — per-domain downstream tool servers (real runnable tools)
# --------------------------------------------------------------------------- #


async def test_sample_domain_runs():
    from devai.mcphub.tool_server import sample_domain

    tools = {t.name: t for t in sample_domain()}
    assert set(tools) == {"sample_ping", "sample_echo"}
    assert await tools["sample_ping"].handler({}) == "pong"
    assert await tools["sample_echo"].handler({"text": "hi"}) == "hi"


def test_scm_domain_binds_real_tools(monkeypatch):
    import devai.scm.factory as scm_factory
    from devai.config import Settings
    from devai.mcphub import tool_server

    monkeypatch.setattr(scm_factory, "create_scm_client", lambda _cfg: object())
    tools = tool_server.scm_domain(Settings())
    names = {t.name for t in tools}
    # Every exposed name is a real registry scm_* tool with a bound handler.
    assert names and all(n.startswith("scm_") for n in names)
    assert "scm_get_file_content" in names
    assert "scm_create_pull_request" in names
    assert all(callable(t.handler) for t in tools)


def test_scm_domain_degrades_without_client(monkeypatch):
    import devai.scm.factory as scm_factory
    from devai.config import Settings
    from devai.mcphub import tool_server

    def _boom(_cfg):
        raise RuntimeError("no creds")

    monkeypatch.setattr(scm_factory, "create_scm_client", _boom)
    assert tool_server.scm_domain(Settings()) == []


def test_build_domains_includes_sample(monkeypatch):
    import devai.scm.factory as scm_factory
    from devai.config import Settings
    from devai.mcphub import tool_server

    monkeypatch.setattr(scm_factory, "create_scm_client", lambda _cfg: object())
    domains = tool_server.build_domains(Settings())
    assert "sample" in domains and "scm" in domains
    assert len(domains["scm"]) >= 10


async def test_hub_leg_stays_routable_across_refreshes(hub):
    """Regression: re-discovery hands `_ensure_connected` a FRESH spec whose
    health is the registry default — adopting it verbatim flipped every
    connected leg to unhealthy one refresh later, so tools/call answered
    'downstream unavailable' while the session underneath was fine."""
    await hub.refresh()
    await hub.refresh()  # second discovery → fresh specs for live legs
    result = await hub.call_tool("sre-mcp__list_pods", {"ns": "devai"})
    assert result.content == [{"echo": "list_pods", "args": {"ns": "devai"}}]
