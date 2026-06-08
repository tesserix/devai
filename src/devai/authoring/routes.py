"""FastAPI routes for authoring agents and blueprints.

Mounted at ``/api/authoring/*``. Create endpoints validate through the
strict loaders and return 422 on bad input; everything 503s cleanly when
the service isn't wired.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devai.authoring.service import AuthoringError, AuthoringQuotaError
from devai.identity import extract_principal

if TYPE_CHECKING:
    from devai.authoring.service import AuthoringService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/authoring", tags=["authoring"])


def _service(request: Request) -> AuthoringService:
    svc = getattr(request.app.state, "authoring_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="authoring service unavailable")
    return svc


def _require_auth(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    return bool(getattr(settings, "require_auth", False))


async def _principal(request: Request) -> str:
    """Resolve the authoring principal for attribution.

    Honest attribution (CODE-21): when ``require_auth`` is on, a request with
    no resolvable principal is rejected (401) instead of being silently
    attributed to ``operator``. We also distinguish a genuine "no principal"
    (anonymous request) from a "lookup error" (identity backend hiccup) in
    the logs. Default ``require_auth=False`` preserves today's behavior —
    anonymous creates are attributed to ``operator``.
    """
    lookup_failed = False
    try:
        p = await extract_principal(request)
    except Exception:  # noqa: BLE001
        logger.warning("authoring: principal lookup raised — treating as anonymous", exc_info=True)
        p = None
        lookup_failed = True
    if p is not None:
        for attr in ("email", "display_name", "uid"):
            val = getattr(p, attr, None)
            if val:
                return str(val)
    if _require_auth(request):
        raise HTTPException(status_code=401, detail="authentication required for authoring")
    if lookup_failed:
        logger.info("authoring: attributing to 'operator' after identity lookup error")
    else:
        logger.debug("authoring: anonymous request attributed to 'operator'")
    return "operator"


# ── Agents (specializations) ──────────────────────────────────────────


class HandoverFieldSpec(BaseModel):
    type: str = "string"
    required: bool = True
    description: str = ""


class CreateAgentBody(BaseModel):
    # Either pass raw `yaml`, or the structured fields below.
    yaml: str = ""
    name: str = Field("", max_length=100)
    display_name: str = Field("", max_length=200)
    description: str = Field("", max_length=2000)
    category: str = "specialist"
    llm_provider: str = "auto"
    llm_model: str = ""
    temperature: float | None = None
    system_prompt: str = Field("", max_length=20000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    handover_schema: dict[str, HandoverFieldSpec] = Field(default_factory=dict)
    risk_level: str = ""
    output_key: str = ""
    # Registry-composition picks from the shared catalog (names). These ride
    # along on the spec's metadata and become the published Agent's ref lists.
    skills: list[str] = Field(default_factory=list, max_length=100)
    prompts: list[str] = Field(default_factory=list, max_length=100)
    mcp_servers: list[str] = Field(default_factory=list, max_length=100)


@router.get("/specializations")
async def list_specializations(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_specializations()


@router.get("/specializations/{name}")
async def get_specialization(request: Request, name: str) -> dict[str, Any]:
    row = await _service(request).get_specialization(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return row


@router.post("/specializations", status_code=201)
async def create_specialization(request: Request, body: CreateAgentBody) -> dict[str, Any]:
    svc = _service(request)
    created_by = await _principal(request)
    try:
        if body.yaml.strip():
            return await svc.create_specialization(body.yaml, created_by=created_by)
        if not body.name:
            raise HTTPException(status_code=422, detail="name (or yaml) is required")
        fields = body.model_dump()
        fields["handover_schema"] = {
            k: v.model_dump() if isinstance(v, HandoverFieldSpec) else v
            for k, v in (body.handover_schema or {}).items()
        }
        return await svc.create_specialization_from_fields(fields, created_by=created_by)
    except AuthoringQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/specializations/{name}")
async def delete_specialization(request: Request, name: str) -> dict[str, Any]:
    deleted = await _service(request).delete_specialization(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return {"deleted": name}


# ── Blueprints (workflows) ────────────────────────────────────────────


class CreateBlueprintBody(BaseModel):
    yaml: str = Field(..., min_length=1, max_length=100000)


@router.get("/blueprints")
async def list_blueprints(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_blueprints()


@router.get("/blueprints/{name}")
async def get_blueprint(request: Request, name: str) -> dict[str, Any]:
    row = await _service(request).get_blueprint(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
    return row


@router.post("/blueprints", status_code=201)
async def create_blueprint(request: Request, body: CreateBlueprintBody) -> dict[str, Any]:
    svc = _service(request)
    try:
        return await svc.create_blueprint(body.yaml, created_by=await _principal(request))
    except AuthoringQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/blueprints/{name}")
async def delete_blueprint(request: Request, name: str) -> dict[str, Any]:
    deleted = await _service(request).delete_blueprint(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
    return {"deleted": name}


# ── Skills ────────────────────────────────────────────────────────────


class CreateSkillBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    title: str = Field("", max_length=200)
    description: str = Field("", max_length=2000)
    category: str = ""
    tags: list[str] = Field(default_factory=list, max_length=50)
    repository: str = Field("", max_length=500)


@router.get("/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_skills()


@router.post("/skills", status_code=201)
async def create_skill(request: Request, body: CreateSkillBody) -> dict[str, Any]:
    try:
        return await _service(request).create_skill(body.model_dump(), created_by=await _principal(request))
    except AuthoringQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/skills/{name}")
async def delete_skill(request: Request, name: str) -> dict[str, Any]:
    if not await _service(request).delete_skill(name):
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    return {"deleted": name}


# ── Custom tools (MCP servers) ────────────────────────────────────────


class CreateMcpServerBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    version: str = Field("1.0.0", max_length=50)
    description: str = Field("", max_length=2000)
    # Remote mode: a reachable MCP endpoint.
    url: str = Field("", max_length=1000)
    transport: str = "streamableHttp"
    headers: dict[str, str] = Field(default_factory=dict)
    # Image mode: a container the platform runs in the MCP sandbox.
    image: str = Field("", max_length=500)
    command: str = Field("", max_length=500)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)


@router.get("/mcp-servers")
async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_mcp_servers()


@router.post("/mcp-servers", status_code=201)
async def create_mcp_server(request: Request, body: CreateMcpServerBody) -> dict[str, Any]:
    if not body.url and not body.image:
        raise HTTPException(status_code=422, detail="one of 'url' (remote) or 'image' (container) is required")
    try:
        return await _service(request).create_mcp_server(body.model_dump(), created_by=await _principal(request))
    except AuthoringQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/mcp-servers/{name}")
async def delete_mcp_server(request: Request, name: str) -> dict[str, Any]:
    if not await _service(request).delete_mcp_server(name):
        raise HTTPException(status_code=404, detail=f"mcp server {name!r} not found")
    return {"deleted": name}


__all__ = ["router"]
