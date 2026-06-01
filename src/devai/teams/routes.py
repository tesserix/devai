"""FastAPI routes for teams + crews — mounted at /api/teams.

Reads the `TeamService` off `request.app.state.team_service` (wired in the
app factory; see Phase 5). When teams aren't wired (or the tables aren't
bootstrapped) every endpoint returns an empty/safe result rather than 500
— teams are additive.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/teams", tags=["teams"])


def _svc(request: Request) -> Any:
    svc = getattr(request.app.state, "team_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="teams are not enabled")
    return svc


def _principal(request: Request):
    # Lazy import to avoid a hard dependency at module import time.
    from devai.identity import extract_principal

    return extract_principal(request)


# ── Teams ─────────────────────────────────────────────────────────────


@router.get("")
async def list_teams(request: Request) -> list[dict[str, Any]]:
    return await _svc(request).list_teams()


class CreateTeamBody(BaseModel):
    name: str = Field(..., min_length=1)
    org_id: str = ""


@router.post("", status_code=201)
async def create_team(request: Request, body: CreateTeamBody) -> dict[str, str]:
    principal = await _principal(request)
    creator = (principal.uid or principal.email) if principal else "system@devai"
    team_id = await _svc(request).create_team(body.name, created_by=creator, org_id=body.org_id)
    return {"team_id": team_id}


@router.get("/{team_id}")
async def get_team(request: Request, team_id: str) -> dict[str, Any]:
    team = await _svc(request).get_team(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    return team


# ── Members ───────────────────────────────────────────────────────────


@router.get("/{team_id}/members")
async def list_members(request: Request, team_id: str) -> list[dict[str, Any]]:
    return await _svc(request).members(team_id)


class AddMemberBody(BaseModel):
    user_uid: str = Field(..., min_length=1)
    roles: list[str] = Field(default_factory=list)


@router.post("/{team_id}/members", status_code=201)
async def add_member(request: Request, team_id: str, body: AddMemberBody) -> dict[str, bool]:
    ok = await _svc(request).add_member(team_id, body.user_uid, body.roles)
    return {"ok": ok}


# ── Crews ─────────────────────────────────────────────────────────────


@router.get("/{team_id}/crews")
async def list_crews(request: Request, team_id: str) -> list[dict[str, Any]]:
    return await _svc(request).list_crews(team_id)


class CreateCrewBody(BaseModel):
    name: str = Field(..., min_length=1)
    members: list[dict[str, Any]] = Field(..., description="[{specialization, role_label, allowed_tools?}]")
    lead: str = Field(..., description="specialization name of the crew lead")
    description: str = ""


@router.post("/{team_id}/crews", status_code=201)
async def create_crew(request: Request, team_id: str, body: CreateCrewBody) -> dict[str, str]:
    if not any(m.get("specialization") == body.lead for m in body.members):
        raise HTTPException(status_code=400, detail="lead must be one of the crew members")
    crew_id = await _svc(request).create_crew(
        team_id, body.name, members=body.members, lead=body.lead, description=body.description
    )
    return {"crew_id": crew_id}


__all__ = ["router"]
