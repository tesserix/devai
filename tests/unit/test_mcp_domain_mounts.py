"""The per-domain MCP mounts must serve the seed endpoint VERBATIM.

MCPServer seeds say ``…/mcp/scm`` (no trailing slash). A Starlette mount
strips its prefix, handing the wrapped ASGI app path ``""`` for the exact
URL — which the streamable-HTTP manager 404s, and the MCP SDK client (no
redirect following) surfaces as "Session terminated" at the Hub. The
mount wrapper normalizes that to "/" so both URL forms serve.
"""

from __future__ import annotations

import pytest

mcp = pytest.importorskip("mcp")

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from devai.mcp_stateless import stateless_asgi  # noqa: E402
from devai.mcphub.tool_server import build_domain_server, sample_domain  # noqa: E402


@pytest.mark.asyncio
async def test_domain_mount_serves_with_and_without_trailing_slash():
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = build_domain_server("sample", sample_domain())
    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    app = Starlette()

    app.mount("/mcp/sample", stateless_asgi(manager.handle_request, normalize_mount=True))

    async with manager.run():
        with TestClient(app) as client:
            for path in ("/mcp/sample", "/mcp/sample/"):
                resp = client.post(
                    path,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "sample_ping",
                            "arguments": {},
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                                "io.modelcontextprotocol/clientInfo": {
                                    "name": "devai-test",
                                    "version": "1",
                                },
                            },
                        },
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "MCP-Method": "tools/call",
                        "MCP-Name": "sample_ping",
                    },
                )
                assert resp.status_code == 200, f"{path} → {resp.status_code}: {resp.text[:200]}"
                assert "pong" in resp.text, f"{path} did not reach the tool: {resp.text[:200]}"


@pytest.mark.asyncio
async def test_domain_mount_enforces_stateless_2026_contract():
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(app=build_domain_server("sample", sample_domain()), stateless=True)
    app = Starlette()
    app.mount("/mcp", stateless_asgi(manager.handle_request))
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "devai-test", "version": "1"},
    }

    async with manager.run():
        with TestClient(app) as client:
            discovered = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": {"_meta": metadata},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "server/discover",
                },
            )
            assert discovered.status_code == 200
            assert "2026-07-28" in discovered.text
            assert "Mcp-Session-Id" not in discovered.headers

            session = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {"_meta": metadata},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/list",
                    "Mcp-Session-Id": "forbidden",
                },
            )
            assert session.status_code == 404
            assert client.get("/mcp/").status_code == 405
