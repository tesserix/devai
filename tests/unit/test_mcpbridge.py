"""stdio MCP bridge — pure logic (no MCP SDK needed).

Covers the launch-spec env substitution, the command allowlist, and the
registry catalog → LaunchSpec mapping. The live spawn/proxy path needs the
mcp SDK + node and is exercised in-cluster.
"""

from __future__ import annotations

from types import SimpleNamespace

from devai.mcpbridge.app import load_catalog_specs
from devai.mcpbridge.runner import LaunchSpec, command_allowed


def test_command_allowlist():
    assert command_allowed("npx", ["npx"]) is True
    assert command_allowed("/usr/bin/npx", ["npx"]) is True
    assert command_allowed("bash", ["npx"]) is False
    assert command_allowed("anything", ["*"]) is True


def test_resolve_env_substitutes_secret_and_prefs():
    spec = LaunchSpec(
        command="npx",
        args=["-y", "x"],
        env={"DATABASE_URL": "{secret}", "TEAM": "{prefs:team_id}", "STATIC": "v"},
    )
    env = spec.resolve_env(secret="postgres://...", prefs={"team_id": "T1"})
    assert env == {"DATABASE_URL": "postgres://...", "TEAM": "T1", "STATIC": "v"}


def test_resolve_env_drops_unresolved_placeholders():
    spec = LaunchSpec(command="npx", env={"DATABASE_URL": "{secret}", "TEAM": "{prefs:missing}"})
    env = spec.resolve_env(secret="", prefs={})
    assert env == {}  # no secret, no pref → both dropped, server gets nothing


def test_load_catalog_specs_picks_stdio_catalog_only():
    def rec(name, labels, raw_extra):
        return SimpleNamespace(name=name, raw={"metadata": {"labels": labels}, **raw_extra})

    class _Client:
        def list_mcp_servers(self):
            return [
                # stdio catalog → included, keyed by endpoint segment
                rec(
                    "catalog-drawio-mcp",
                    {"mcp.devai.io/catalog": "true"},
                    {
                        "endpoint": "http://devai-mcp-bridge.devai.svc.cluster.local:8099/bridge/drawio",
                        "stdio": {"command": "npx", "args": ["-y", "drawio-mcp-server"]},
                    },
                ),
                # http catalog (no stdio) → skipped
                rec("catalog-github-mcp", {"mcp.devai.io/catalog": "true"}, {"endpoint": "https://api.githubcopilot.com/mcp/"}),
                # non-catalog server → skipped
                rec("gitops-mcp", {"devai.io/source": "devai"}, {"endpoint": "http://x/mcp", "stdio": {"command": "npx"}}),
            ]

    specs = load_catalog_specs(_Client())
    assert set(specs) == {"drawio"}
    assert specs["drawio"].command == "npx"
    assert specs["drawio"].args == ["-y", "drawio-mcp-server"]


def test_load_catalog_specs_registry_error_is_empty():
    class _Boom:
        def list_mcp_servers(self):
            raise RuntimeError("down")

    assert load_catalog_specs(_Boom()) == {}


def test_personal_bridge_endpoint_uses_x_mcp_secret():
    from devai.mcphub.personal import _is_bridge_endpoint, _spec_for

    assert _is_bridge_endpoint("http://devai-mcp-bridge.devai.svc.cluster.local:8099/bridge/postgres") is True
    assert _is_bridge_endpoint("https://api.acme.dev/mcp") is False

    spec = _spec_for(
        {
            "instance_id": "postgres",
            "provider": "streamable_http",
            "mcp_url": "http://devai-mcp-bridge.devai.svc.cluster.local:8099/bridge/postgres",
            "mcp_token": "postgres://u:p@h/db",
        }
    )
    assert spec is not None
    # Secret goes to the bridge via x-mcp-secret (not Authorization), and the
    # in-cluster bridge bypassed the external SSRF guard.
    assert spec.headers["x-mcp-secret"] == "postgres://u:p@h/db"
    assert "Authorization" not in spec.headers
