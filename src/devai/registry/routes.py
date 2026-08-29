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
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request

from devai.agentic.runtime_status import build_agent_runtime_snapshot, fetch_controller_inventory
from devai.authz import require_principal
from devai.identity import extract_principal
from devai.orchestration.agent_lifecycle_http import durable_result, require_idempotency_key
from devai.registry.client import (
    Agent,
    Dataset,
    EvalSuite,
    McpServer,
    Prompt,
    RegistryClient,
    RegistryError,
    Skill,
)
from devai.registry.promotion import (
    AgentPromotionError,
    AgentPromotionService,
    PromotionOptions,
    editable_agent_manifest,
)
from devai.registry.semantic import OWNER_LABEL, RegistrySemanticSearch, principal_owner_id

if TYPE_CHECKING:
    from devai.identity import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])

_OWNER_LABEL = OWNER_LABEL
_VISIBILITY_LABEL = "devai.tesserix.app/visibility"
_GATE_OVERRIDE_HEADER = "x-devai-eval-gate-override"
_GATE_OVERRIDE_REASON_HEADER = "x-devai-eval-gate-override-reason"

type RegistryItem = Skill | Prompt | McpServer | Agent | Dataset | EvalSuite


def _client(request: Request) -> RegistryClient:
    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="registry client not configured — set DEVAI_REGISTRY_URL",
        )
    return cast("RegistryClient", client)


def _to_dict(item: RegistryItem) -> dict[str, Any]:
    """Convert dataclass → dict, dropping the raw passthrough field.

    `raw` is useful for stage code that wants the full record but ugly
    on the wire. The dashboard never needs it; drop it.
    """
    d = asdict(item)
    d.pop("raw", None)
    return d


async def _optional_principal(request: Request) -> Principal | None:
    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001
        logger.warning("registry: principal lookup failed", exc_info=True)
        return None


def _owner_id(principal: Principal | None) -> str:
    return principal_owner_id(principal)


def _labels(item: RegistryItem | dict[str, Any]) -> dict[str, str]:
    if isinstance(item, dict):
        metadata = item.get("metadata")
        raw_labels = metadata.get("labels") if isinstance(metadata, dict) else None
    else:
        raw_labels = getattr(item, "raw", {}).get("labels")
        if raw_labels is None:
            raw_labels = getattr(item, "labels", {})
    if not isinstance(raw_labels, dict):
        return {}
    return {str(key): str(value) for key, value in raw_labels.items()}


def _visibility(item: RegistryItem | dict[str, Any]) -> str:
    if isinstance(item, dict):
        metadata = item.get("metadata")
        value = metadata.get("visibility") if isinstance(metadata, dict) else ""
    else:
        value = getattr(item, "raw", {}).get("visibility", "")
    return str(value or "").strip().lower()


def _visible(item: RegistryItem | dict[str, Any], principal: Principal | None) -> bool:
    owner = _labels(item).get(_OWNER_LABEL, "")
    if owner:
        return owner == _owner_id(principal)
    return _visibility(item) != "private"


async def _visible_items[T: RegistryItem](
    request: Request,
    loader: Callable[[], list[T]],
) -> list[T]:
    principal, items = await asyncio.gather(
        _optional_principal(request),
        asyncio.to_thread(loader),
    )
    return [item for item in items if _visible(item, principal)]


async def _visible_item[T: RegistryItem](
    request: Request,
    loader: Callable[[str], T | None],
    name: str,
) -> T | None:
    principal = await _optional_principal(request)
    item = await asyncio.to_thread(loader, name)
    if item is None or not _visible(item, principal):
        return None
    return item


async def _visible_envelope(
    request: Request,
    client: RegistryClient,
    plural: str,
    name: str,
    tag: str = "",
) -> dict[str, Any] | None:
    principal = await _optional_principal(request)
    if tag:
        item = await asyncio.to_thread(client.get_artifact_envelope, plural, name, tag)
    else:
        item = await asyncio.to_thread(client.get_artifact_envelope, plural, name)
    if item is None or not _visible(item, principal):
        return None
    return item


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
        skills, prompts, servers, agents, datasets, eval_suites = await asyncio.gather(
            _visible_items(request, client.list_skills),
            _visible_items(request, client.list_prompts),
            _visible_items(request, client.list_mcp_servers),
            _visible_items(request, client.list_agents),
            _visible_items(request, client.list_datasets),
            _visible_items(request, client.list_eval_suites),
        )
        return {
            "skills": len(skills),
            "prompts": len(prompts),
            "mcp_servers": len(servers),
            "agents": len(agents),
            "datasets": len(datasets),
            "eval_suites": len(eval_suites),
        }
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


def _search_service(request: Request, client: RegistryClient) -> RegistrySemanticSearch:
    service = getattr(request.app.state, "registry_search_service", None)
    if isinstance(service, RegistrySemanticSearch):
        return service
    service = RegistrySemanticSearch(client)
    request.app.state.registry_search_service = service
    return service


@router.get("/search")
async def search_registry(
    request: Request,
    q: str,
    kinds: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Find authorized registry artifacts by meaning and structured metadata."""
    client = _client(request)
    principal = await _optional_principal(request)
    selected_kinds = [part.strip() for part in kinds.split(",") if part.strip()] or None
    try:
        result = await _search_service(request, client).search(
            q,
            principal=principal,
            kinds=selected_kinds,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RegistryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return result.to_dict()


@router.get("/artifacts/{plural}/{name:path}")
async def get_registry_artifact(
    request: Request,
    plural: str,
    name: str,
    tag: str = "",
) -> dict[str, Any]:
    """Progressively fetch one exact search hit after object authorization."""
    allowed = {
        "tools",
        "skills",
        "agents",
        "mcp-servers",
        "prompts",
        "workflows",
        "blueprints",
        "datasets",
        "eval-suites",
    }
    if plural not in allowed:
        raise HTTPException(status_code=404, detail=f"registry collection not found: {plural}")
    client = _client(request)
    try:
        item = await _visible_envelope(request, client, plural, name, tag)
    except RegistryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if item is None:
        raise HTTPException(status_code=404, detail=f"registry artifact not found: {name}")
    return item


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await _visible_items(request, client.list_skills)
        return [_to_dict(s) for s in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/skills/{name}")
async def get_skill(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await _visible_item(request, client.get_skill, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return _to_dict(item)


@router.get("/prompts")
async def list_prompts(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await _visible_items(request, client.list_prompts)
        return [_to_dict(p) for p in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/prompts/{name}")
async def get_prompt(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await _visible_item(request, client.get_prompt, name)
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
        items = await _visible_items(request, client.list_mcp_servers)
        return [_to_dict(s) for s in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/mcp-servers/{name:path}")
async def get_mcp_server(request: Request, name: str) -> dict[str, Any]:
    # MCP server names are publisher/server (a slash in the name). Use
    # FastAPI's ``:path`` converter so the slash isn't a route boundary.
    client = _client(request)
    try:
        item = await _visible_item(request, client.get_mcp_server, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"mcp server not found: {name}")
    return _to_dict(item)


@router.get("/agents")
async def list_agents(request: Request, mine: bool = False) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        if mine:
            principal = await require_principal(request)
            owner_id = _owner_id(principal)
            if not owner_id:
                raise HTTPException(status_code=401, detail="authenticated principal has no stable subject")
            loaded = await asyncio.to_thread(client.list_agents)
            items = [item for item in loaded if _labels(item).get(_OWNER_LABEL) == owner_id]
        else:
            items = await _visible_items(request, client.list_agents)
        return [_to_dict(a) for a in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/agents/runtime-status")
async def agent_runtime_status(request: Request, mine: bool = False) -> dict[str, Any]:
    """Return live runtime evidence for only the agents visible to the caller."""
    principal = await require_principal(request)
    client = _client(request)
    try:
        loaded = await asyncio.to_thread(client.list_agents)
    except RegistryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    if mine:
        owner_id = _owner_id(principal)
        if not owner_id:
            raise HTTPException(status_code=401, detail="authenticated principal has no stable subject")
        agents = [item for item in loaded if _labels(item).get(_OWNER_LABEL) == owner_id]
    else:
        agents = [item for item in loaded if _visible(item, principal)]

    from devai.config import settings as base_settings
    from devai.settings.overlay import build_overlay

    effective = await build_overlay(
        getattr(request.app.state, "config", base_settings),
        principal,
        getattr(request.app.state, "settings_service", None),
    )
    inventory = await fetch_controller_inventory(str(getattr(effective, "kagent_url", "") or ""))
    return build_agent_runtime_snapshot(
        agents,
        inventory,
        substrate_enabled=bool(getattr(effective, "kagent_enabled", False)),
        namespace=str(getattr(effective, "kagent_default_namespace", "") or "kagent-system"),
    )


@router.get("/agents/{name}")
async def get_agent(request: Request, name: str) -> dict[str, Any]:
    client = _client(request)
    try:
        item = await _visible_item(request, client.get_agent, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {name}")
    return _to_dict(item)


@router.get("/datasets")
async def list_datasets(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await _visible_items(request, client.list_datasets)
        return [_to_dict(item) for item in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/eval-suites")
async def list_eval_suites(request: Request) -> list[dict[str, Any]]:
    client = _client(request)
    try:
        items = await _visible_items(request, client.list_eval_suites)
        return [_to_dict(item) for item in items]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/agents/{name}/manifest")
async def get_owned_agent_manifest(request: Request, name: str) -> dict[str, Any]:
    """Return an editable manifest only to the agent's authenticated owner."""
    principal = await require_principal(request)
    owner_id = _owner_id(principal)
    if not owner_id:
        raise HTTPException(status_code=401, detail="authenticated principal has no stable subject")
    client = _client(request)
    try:
        existing = await asyncio.to_thread(client.get_artifact_envelope, "agents", name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if existing is None or _labels(existing).get(_OWNER_LABEL) != owner_id:
        raise HTTPException(status_code=404, detail=f"agent not found: {name}")

    return editable_agent_manifest(existing)


@router.post("/refresh")
async def refresh(request: Request) -> dict[str, str]:
    """Invalidate the in-process cache. Dashboard 'Refresh catalog'
    button + the registry-bootstrap Job's post-run hook both call this."""
    await require_principal(request)
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
    "datasets": "publish_dataset",
    "eval-suites": "publish_eval_suite",
    "blueprints": "publish_blueprint",
    "workflows": "publish_workflow",
    "tools": "publish_tool",
}


@router.post("/{plural}", status_code=201)
async def publish(request: Request, plural: str) -> dict[str, Any]:
    """Publish a registry CR manifest (apiVersion/kind/metadata/spec) — the
    write path behind the dashboard's artifact editor. Mirrors aregistry's
    POST /v0/{plural}.

    User-authored artifacts stay in the platform catalog namespace but are
    private by default. DevAI stamps an opaque owner label derived from the
    verified principal, filters reads by that owner, and checks the same label
    before versioning. Pass ``?overwrite=true`` to publish a new version of an
    artifact the caller already owns. Team is optional grouping metadata, never
    an authorization identity."""
    method = _PUBLISH_METHOD.get(plural)
    if method is None:
        raise HTTPException(status_code=404, detail=f"unknown registry kind: {plural}")
    client = _client(request)
    principal = await require_principal(request)
    owner_id = _owner_id(principal)
    if not owner_id:
        raise HTTPException(status_code=401, detail="authenticated principal has no stable subject")
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="manifest must be a JSON object")
    if plural == "agents":
        key = require_idempotency_key(request, request.headers.get("Idempotency-Key"))
        durable = await durable_result(
            request,
            "agent promotion",
            lambda orchestrator: orchestrator.promote(
                principal,
                {
                    "manifest": deepcopy(body),
                    "overwrite": request.query_params.get("overwrite", "").lower() in ("1", "true", "yes"),
                    "allow_gate_override": request.headers.get(_GATE_OVERRIDE_HEADER, "").lower()
                    in ("1", "true", "yes"),
                    "override_reason": request.headers.get(_GATE_OVERRIDE_REASON_HEADER, ""),
                },
                request_id=key,
                requires_approval=False,
            ),
        )
        if durable is not None:
            return durable
        try:
            return await AgentPromotionService(
                client,
                getattr(request.app.state, "agent_gate_service", None),
            ).promote(
                principal,
                body,
                options=PromotionOptions(
                    overwrite=request.query_params.get("overwrite", "").lower() in ("1", "true", "yes"),
                    allow_gate_override=request.headers.get(_GATE_OVERRIDE_HEADER, "").lower() in ("1", "true", "yes"),
                    override_reason=request.headers.get(_GATE_OVERRIDE_REASON_HEADER, ""),
                ),
            )
        except AgentPromotionError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
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
    labels = meta.get("labels")
    if labels is None:
        labels = {}
        meta["labels"] = labels
    if not isinstance(labels, dict):
        raise HTTPException(status_code=400, detail="manifest.metadata.labels must be an object")
    labels[_OWNER_LABEL] = owner_id
    labels[_VISIBILITY_LABEL] = "private"
    meta["visibility"] = "private"

    name = (meta.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="manifest.metadata.name is required")
    overwrite = request.query_params.get("overwrite", "").lower() in ("1", "true", "yes")
    try:
        existing = await asyncio.to_thread(client.get_artifact_envelope, plural, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if existing is not None:
        if _labels(existing).get(_OWNER_LABEL) != owner_id:
            raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
        if not overwrite:
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
    response = dict(result) if isinstance(result, dict) else {"status": "published"}
    return response


@router.delete("/{plural}/{name:path}")
async def unpublish(request: Request, plural: str, name: str) -> dict[str, str]:
    """Delete only an artifact owned by the authenticated caller."""
    if plural not in _PUBLISH_METHOD:
        raise HTTPException(status_code=404, detail=f"unknown registry kind: {plural}")
    principal = await require_principal(request)
    owner_id = _owner_id(principal)
    if not owner_id:
        raise HTTPException(status_code=401, detail="authenticated principal has no stable subject")
    client = _client(request)
    try:
        existing = await asyncio.to_thread(client.get_artifact_envelope, plural, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if existing is None or _labels(existing).get(_OWNER_LABEL) != owner_id:
        raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
    try:
        await asyncio.to_thread(client.delete, plural, name)
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    client.refresh()
    return {"deleted": name}
