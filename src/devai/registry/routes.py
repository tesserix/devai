"""FastAPI routes for the aregistry catalog.

Mounted at ``/api/registry/*`` by the webhook app. Backed by the
process-wide ``app.state.registry_client`` (constructed at startup
from ``settings.registry_url``).

When the client is ``None`` (registry disabled), every endpoint returns
HTTP 503 with a structured body so the dashboard can render a "registry
not configured" empty state without guessing from a 404.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from devai.registry.client import (
    Agent,
    McpServer,
    Prompt,
    RegistryClient,
    RegistryError,
    Skill,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])


def _client(request: Request) -> RegistryClient:
    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="registry client not configured — set DEVAI_REGISTRY_URL",
        )
    return client


def _to_dict(item: Skill | Prompt | McpServer | Agent) -> dict[str, Any]:
    """Convert dataclass → dict, dropping the raw passthrough field.

    `raw` is useful for stage code that wants the full record but ugly
    on the wire. The dashboard never needs it; drop it.
    """
    d = asdict(item)
    d.pop("raw", None)
    return d


@router.get("/health")
async def registry_health(request: Request) -> dict[str, Any]:
    """Probe the registry. Returns ``reachable: false`` instead of a
    5xx so dashboards can render an indicator."""
    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        return {"reachable": False, "error": "client not configured"}
    return await asyncio.to_thread(client.health)


@router.get("/counts")
async def registry_counts(request: Request) -> dict[str, int]:
    client = _client(request)
    try:
        return await asyncio.to_thread(client.counts)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await asyncio.to_thread(client.list_skills)
        return [_to_dict(s) for s in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/skills/{name}")
async def get_skill(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await asyncio.to_thread(client.get_skill, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return _to_dict(item)


@router.get("/prompts")
async def list_prompts(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await asyncio.to_thread(client.list_prompts)
        return [_to_dict(p) for p in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/prompts/{name}")
async def get_prompt(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await asyncio.to_thread(client.get_prompt, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"prompt not found: {name}")
    return _to_dict(item)


@router.get("/mcp-servers")
async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
    """List MCP servers. DevAI keeps the 'mcp-servers' tree-name even
    though aregistry's HTTP endpoint is /v0/servers; the dashboard
    treats them as MCP-only."""
    client = _client(request)
    try:
        items = await asyncio.to_thread(client.list_mcp_servers)
        return [_to_dict(s) for s in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/mcp-servers/{name:path}")
async def get_mcp_server(request: Request, name: str) -> dict[str, Any]:
    # MCP server names are publisher/server (a slash in the name). Use
    # FastAPI's ``:path`` converter so the slash isn't a route boundary.
    client = _client(request)
    try:
        item = await asyncio.to_thread(client.get_mcp_server, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"mcp server not found: {name}")
    return _to_dict(item)


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await asyncio.to_thread(client.list_agents)
        return [_to_dict(a) for a in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/agents/{name}")
async def get_agent(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await asyncio.to_thread(client.get_agent, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {name}")
    return _to_dict(item)


@router.post("/refresh")
async def refresh(request: Request) -> dict[str, str]:
    """Invalidate the in-process cache. Dashboard 'Refresh catalog'
    button + the registry-bootstrap Job's post-run hook both call this."""
    client = _client(request)
    client.refresh()
    return {"status": "cache cleared"}
