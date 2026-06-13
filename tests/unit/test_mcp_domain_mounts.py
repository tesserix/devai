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

from devai.mcphub.tool_server import build_domain_server, sample_domain  # noqa: E402


@pytest.mark.asyncio
async def test_domain_mount_serves_with_and_without_trailing_slash():
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = build_domain_server("sample", sample_domain())
    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    app = Starlette()

    # Mirrors mount_domain_servers' wrapper (kept in sync by this test).
    def _asgi(scope, receive, send, _m=manager):
        if scope.get("type") == "http" and scope.get("path", "") in ("", scope.get("root_path", "")):
            scope = dict(scope)
            scope["path"] = "/"
        return _m.handle_request(scope, receive, send)

    app.mount("/mcp/sample", _asgi)

    async with manager.run():
        with TestClient(app) as client:
            for path in ("/mcp/sample", "/mcp/sample/"):
                resp = client.post(
                    path,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "sample_ping", "arguments": {}},
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
                assert resp.status_code == 200, f"{path} → {resp.status_code}: {resp.text[:200]}"
                assert "pong" in resp.text, f"{path} did not reach the tool: {resp.text[:200]}"
