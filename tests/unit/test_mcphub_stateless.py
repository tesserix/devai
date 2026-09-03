from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from devai.mcp_stateless import stateless_asgi
from devai.mcphub.downstream import DownstreamConnection
from devai.mcphub.model import DownstreamSpec, FederatedTool
from devai.mcphub.server import build_hub_server


@pytest.mark.asyncio
async def test_downstream_connection_uses_stateless_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Session:
        def __init__(self, _read: object, _write: object) -> None:
            pass

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def discover(self) -> object:
            calls.append("server/discover")
            return SimpleNamespace(capabilities=SimpleNamespace(tools=object()))

        async def initialize(self) -> object:
            raise AssertionError("legacy initialize must not be used")

    fake_mcp = ModuleType("mcp")
    fake_mcp.ClientSession = Session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)

    connection = DownstreamConnection(DownstreamSpec("sample", "http://sample.test/mcp"))

    @asynccontextmanager
    async def transport() -> object:
        yield object(), object()

    stack = transport()
    monkeypatch.setattr(connection, "_open_transport", lambda _stack: stack.__aenter__())

    await connection.connect()
    try:
        assert calls == ["server/discover"]
        assert connection.capabilities == {"tools": True}
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_hub_server_lists_and_calls_through_stateless_low_level_api() -> None:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    class Hub:
        async def list_tools_for(self, _principal: object, _profile: object) -> object:
            return SimpleNamespace(
                truncated=False,
                selected=[FederatedTool.build("sample", "ping")],
                dropped_by_budget=[],
            )

        async def call_tool(self, name: str, arguments: dict[str, object], **_kwargs: object) -> str:
            assert name == "sample__ping"
            assert arguments == {}
            return "pong"

        def list_prompts(self) -> list[object]:
            return []

        def list_resources(self) -> list[object]:
            return []

    manager = StreamableHTTPSessionManager(app=build_hub_server(Hub()), stateless=True)
    app = Starlette()
    app.mount("/mcp", stateless_asgi(manager.handle_request))
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "devai-test", "version": "1"},
    }

    async with manager.run():
        with TestClient(app) as client:
            listed = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {"_meta": metadata},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/list",
                },
            )
            assert listed.status_code == 200
            assert "sample__ping" in listed.text

            called = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "sample__ping", "arguments": {}, "_meta": metadata},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/call",
                    "MCP-Name": "sample__ping",
                },
            )
            assert called.status_code == 200
            assert "pong" in called.text
