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
import hashlib
import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from devai.authz import require_principal
from devai.identity import extract_principal
from devai.registry.client import (
    Agent,
    McpServer,
    Prompt,
    RegistryClient,
    RegistryError,
    Skill,
)

if TYPE_CHECKING:
    from devai.identity import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])

_OWNER_LABEL = "devai.tesserix.app/owner-id"
_VISIBILITY_LABEL = "devai.tesserix.app/visibility"
_RUNTIME_LABEL = "devai.io/runtime"
_MODEL_PROVIDERS = frozenset({"anthropic", "claude", "openai", "google", "gemini", "vertex", "vertex_gemini", "groq"})
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})

type RegistryItem = Skill | Prompt | McpServer | Agent


def _agent_contract_errors(body: dict[str, Any]) -> list[str]:
    spec = body.get("spec")
    if not isinstance(spec, dict):
        return ["spec must be an object"]

    errors: list[str] = []
    system_prompt = spec.get("systemPrompt")
    prompt_ref = spec.get("promptRef")
    if system_prompt is not None and not isinstance(system_prompt, str):
        errors.append("spec.systemPrompt must be a string")
    if prompt_ref is not None and not isinstance(prompt_ref, str):
        errors.append("spec.promptRef must be a string")
    has_inline = isinstance(system_prompt, str) and bool(system_prompt.strip())
    has_reference = isinstance(prompt_ref, str) and bool(prompt_ref.strip())
    if not has_inline and not has_reference:
        errors.append("spec.systemPrompt or spec.promptRef is required")

    model = spec.get("model")
    if not isinstance(model, dict):
        errors.append("spec.model must be an object")
    else:
        if not isinstance(model.get("provider"), str) or not model["provider"].strip():
            errors.append("spec.model.provider is required")
        elif model["provider"] not in _MODEL_PROVIDERS:
            errors.append(f"spec.model.provider must be one of {', '.join(sorted(_MODEL_PROVIDERS))}")
        if not isinstance(model.get("name"), str) or not model["name"].strip():
            errors.append("spec.model.name is required")
        temperature = model.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool) or not isinstance(temperature, int | float) or not 0 <= temperature <= 2
        ):
            errors.append("spec.model.temperature must be a number between 0 and 2")

    limits = spec.get("limits")
    if not isinstance(limits, dict):
        errors.append("spec.limits must be an object")
    else:
        max_turns = limits.get("maxTurns")
        if type(max_turns) is not int or not 1 <= max_turns <= 1000:
            errors.append("spec.limits.maxTurns must be an integer between 1 and 1000")
        timeout_seconds = limits.get("timeoutSeconds")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            errors.append("spec.limits.timeoutSeconds must be an integer between 1 and 86400")

    if spec.get("riskLevel") not in _RISK_LEVELS:
        errors.append("spec.riskLevel must be one of low, medium, high, critical")
    return errors


def _prompt_system_message(prompt: Prompt) -> str:
    value = prompt.raw.get("systemPrompt") if isinstance(prompt.raw, dict) else None
    return value.strip() if isinstance(value, str) else ""


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


async def _optional_principal(request: Request) -> Principal | None:
    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001
        logger.warning("registry: principal lookup failed", exc_info=True)
        return None


def _owner_id(principal: Principal | None) -> str:
    if principal is None:
        return ""
    scope = principal.user_scope_id.strip()
    if not scope:
        return ""
    return hashlib.sha256(scope.encode()).hexdigest()[:32]


def _labels(item: Skill | Prompt | McpServer | Agent | dict[str, Any]) -> dict[str, str]:
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


def _visibility(item: Skill | Prompt | McpServer | Agent | dict[str, Any]) -> str:
    if isinstance(item, dict):
        metadata = item.get("metadata")
        value = metadata.get("visibility") if isinstance(metadata, dict) else ""
    else:
        value = getattr(item, "raw", {}).get("visibility", "")
    return str(value or "").strip().lower()


def _visible(item: Skill | Prompt | McpServer | Agent, principal: Principal | None) -> bool:
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
        skills, prompts, servers, agents = await asyncio.gather(
            _visible_items(request, client.list_skills),
            _visible_items(request, client.list_prompts),
            _visible_items(request, client.list_mcp_servers),
            _visible_items(request, client.list_agents),
        )
        return {
            "skills": len(skills),
            "prompts": len(prompts),
            "mcp_servers": len(servers),
            "agents": len(agents),
        }
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


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

    manifest = deepcopy(existing)
    metadata = manifest.get("metadata")
    if isinstance(metadata, dict):
        labels = metadata.get("labels")
        if isinstance(labels, dict):
            metadata["labels"] = {
                key: value for key, value in labels.items() if key not in {_OWNER_LABEL, _VISIBILITY_LABEL}
            }
    spec = manifest.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("promptRef"), str) and spec["promptRef"].strip():
        spec["systemPrompt"] = ""
    return manifest


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
    if plural == "agents" and str(labels.get(_RUNTIME_LABEL, "")).strip().lower() == "kagent":
        raise HTTPException(
            status_code=403,
            detail="user-authored kagent runtime is disabled until the Substrate isolation gate passes",
        )
    if plural == "agents":
        contract_errors = _agent_contract_errors(body)
        if contract_errors:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_agent_manifest", "issues": contract_errors},
            )
        spec = body["spec"]
        prompt_ref = str(spec.get("promptRef") or "").strip()
        if prompt_ref:
            try:
                prompt = await _visible_item(request, client.get_prompt, prompt_ref)
            except RegistryError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e
            if prompt is None:
                raise HTTPException(status_code=404, detail=f"prompt not found: {prompt_ref}")
            system_message = _prompt_system_message(prompt)
            if not system_message:
                raise HTTPException(
                    status_code=400,
                    detail=f"spec.promptRef '{prompt_ref}' has no non-empty spec.systemPrompt",
                )
            spec["systemPrompt"] = system_message
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
    return result if isinstance(result, dict) else {"status": "published"}


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
