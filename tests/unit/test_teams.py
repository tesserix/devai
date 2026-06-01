"""Unit tests for the human-teams layer (Phase 2).

Uses a FakePool that satisfies the asyncpg surface TeamService calls
(fetch/fetchrow/execute), so no Postgres is needed. Also covers the
fail-soft behavior when the tables don't exist yet.
"""

from __future__ import annotations

import pytest

from devai.identity import Principal
from devai.pipeline.types import DevAITask
from devai.services.teams import TeamService


class FakePool:
    """Records SQL and returns scripted rows. Raises if `broken` is set."""

    def __init__(self, *, rows=None, row=None, broken=False) -> None:
        self._rows = rows or []
        self._row = row
        self.broken = broken
        self.executed: list[tuple] = []

    async def fetch(self, query, *args):
        if self.broken:
            raise RuntimeError("relation does not exist")
        return list(self._rows)

    async def fetchrow(self, query, *args):
        if self.broken:
            raise RuntimeError("relation does not exist")
        return self._row

    async def execute(self, query, *args):
        if self.broken:
            raise RuntimeError("relation does not exist")
        self.executed.append((query, args))
        return "INSERT 0 1"


class FakeDB:
    def __init__(self, pool) -> None:
        self.pool = pool


# ── DevAITask + Principal carry team fields ─────────────────────────────


def test_task_serializes_team_fields():
    task = DevAITask(intent="x", team_id="team-abc", crew_id="crew-xyz")
    d = task.to_dict()
    assert d["team_id"] == "team-abc"
    assert d["crew_id"] == "crew-xyz"


def test_principal_team_roundtrip_and_primary():
    p = Principal(email="a@b.com", team_ids=["team-1", "team-2"])
    assert p.primary_team_id == "team-1"
    assert Principal.from_dict(p.to_dict()).team_ids == ["team-1", "team-2"]
    assert Principal(email="x").primary_team_id == ""


# ── TeamService queries ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teams_for_returns_ids():
    pool = FakePool(rows=[{"team_id": "team-1"}, {"team_id": "team-2"}])
    svc = TeamService(FakeDB(pool))
    assert await svc.teams_for("uid-123") == ["team-1", "team-2"]


@pytest.mark.asyncio
async def test_teams_for_empty_key():
    svc = TeamService(FakeDB(FakePool()))
    assert await svc.teams_for("") == []


@pytest.mark.asyncio
async def test_queries_fail_soft_when_tables_missing():
    svc = TeamService(FakeDB(FakePool(broken=True)))
    assert await svc.teams_for("uid") == []
    assert await svc.members("team-1") == []
    assert await svc.list_teams() == []
    assert await svc.list_crews() == []
    assert await svc.get_crew("crew-1") is None


@pytest.mark.asyncio
async def test_create_team_adds_creator_as_admin():
    pool = FakePool()
    svc = TeamService(FakeDB(pool))
    team_id = await svc.create_team("Platform", created_by="uid-1")
    assert team_id.startswith("team-")
    # two writes: the team insert + the creator membership
    assert len(pool.executed) == 2
    assert any("team_members" in q for q, _ in pool.executed)


@pytest.mark.asyncio
async def test_list_crews_decodes_json_members():
    pool = FakePool(
        rows=[{"id": "crew-1", "team_id": "t", "name": "Frontend", "members": '[{"specialization": "ui"}]', "lead": "ui"}]
    )
    svc = TeamService(FakeDB(pool))
    crews = await svc.list_crews("t")
    assert crews[0]["members"] == [{"specialization": "ui"}]


# ── Authorization ───────────────────────────────────────────────────────


def test_can_dispatch_rules():
    svc = TeamService(FakeDB(FakePool()))
    member = Principal(email="a@b.com", team_ids=["team-1"])
    # unscoped run — always allowed
    assert svc.can_dispatch(member, "") is True
    # member of the team
    assert svc.can_dispatch(member, "team-1") is True
    # NOT a member of the requested team
    assert svc.can_dispatch(member, "team-2") is False
    # non-teams user keeps global access (back-compat)
    assert svc.can_dispatch(Principal(email="x"), "team-9") is True
    # synthetic (webhook/cron) principal — unscoped
    assert svc.can_dispatch(None, "team-1") is True
