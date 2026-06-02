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


# plural → RegistryClient.publish_* method. Mirrors aregistry's POST /v0/{plural}.
_PUBLISH_METHOD: dict[str, str] = {
    "skills": "publish_skill",
    "prompts": "publish_prompt",
    "mcp-servers": "publish_mcp_server",
    "servers": "publish_mcp_server",
    "agents": "publish_agent",
    "blueprints": "publish_blueprint",
    "workflows": "publish_workflow",
    "tools": "publish_tool",
}


@router.post("/{plural}", status_code=201)
async def publish(request: Request, plural: str) -> dict[str, Any]:
    """Publish a registry CR manifest (apiVersion/kind/metadata/spec) — the
    write path behind the dashboard's artifact editor. Mirrors aregistry's
    POST /v0/{plural}.

    Tenancy: every artifact is stamped with the tenant namespace, which is the
    isolation + uniqueness boundary. Within a tenant the registry enforces name
    uniqueness across all kinds, so on CREATE we reject a name that already
    exists (a silent same-name publish would otherwise version-bump an existing
    artifact). Pass ``?overwrite=true`` to publish a new version on purpose.
    Team is optional metadata (a label), never part of the identity."""
    method = _PUBLISH_METHOD.get(plural)
    if method is None:
        raise HTTPException(status_code=404, detail=f"unknown registry kind: {plural}")
    client = _client(request)
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="manifest must be a JSON object")
    # Stamp the tenant namespace (the editor doesn't set it) so the artifact is
    # visible to the same scoped reads the catalog uses. An explicit namespace
    # in the manifest must equal the tenant — cross-tenant writes are refused.
    ns = getattr(client, "_namespace", "") or ""
    meta = body.get("metadata")
    if not isinstance(meta, dict):
        raise HTTPException(status_code=400, detail="manifest.metadata is required")
    requested_ns = (meta.get("namespace") or "").strip()
    if ns and requested_ns and requested_ns != ns:
        raise HTTPException(
            status_code=403,
            detail=f"cannot publish into tenant '{requested_ns}' — this DevAI is scoped to '{ns}'",
        )
    if ns:
        meta["namespace"] = ns

    # Enforce name uniqueness within the tenant on create. Skip when the caller
    # explicitly opts into versioning an existing artifact.
    name = (meta.get("name") or "").strip()
    overwrite = request.query_params.get("overwrite", "").lower() in ("1", "true", "yes")
    if name and not overwrite:
        exists = await asyncio.to_thread(client.artifact_exists, plural, name)
        if exists:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{name}' already exists in tenant '{ns or 'default'}'. "
                    "Names are unique within a tenant — pick a different name, "
                    "or republish with overwrite to version the existing artifact."
                ),
            )

    try:
        result = await asyncio.to_thread(getattr(client, method), body)
    except RegistryError as e:
        # The registry's own cross-kind name guard surfaces here as a 4xx in the
        # message; relay it as a conflict so the editor shows a clean error.
        detail = str(e)
        status = 409 if "409" in detail or "conflict" in detail.lower() else 502
        raise HTTPException(status_code=status, detail=detail) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"publish: {e}") from e
    client.refresh()
    return result if isinstance(result, dict) else {"status": "published"}
