"""TeamService — human teams that own AI agent crews.

Two-layer model:
  * a **team** is a group of human users (RBAC / ownership boundary);
  * a team **owns** one or more **agent crews** (named squads of
    specialization "members" with a designated lead) — see Phase 3.

This service owns all team/crew SQL (raw asyncpg, matching the rest of
`services/database.py`). It is deliberately *fail-soft*: the `teams`,
`team_members`, and `agent_crews` tables are provisioned by the
db-schema-bootstrap CronJob in `tesserix-k8s`, so in a fresh dev DB they
may not exist yet. Every read degrades to an empty result and every write
is a no-op-with-warning if the tables are missing — teams are purely
additive and must never break auth or dispatch.

ID scheme mirrors DevAITask: `team-<hex12>` / `crew-<hex12>` (uuid4-based,
so no dependency on the `ulid` package).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.services.database import Database

logger = logging.getLogger(__name__)


class TeamService:
    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def _pool(self) -> Any:
        return self._db.pool

    # ── Membership ────────────────────────────────────────────────────

    async def teams_for(self, user_key: str) -> list[str]:
        """Return the team_ids a user belongs to (by uid, falling back to email)."""
        if not user_key:
            return []
        try:
            rows = await self._pool.fetch(
                "SELECT team_id FROM team_members WHERE user_uid = $1 ORDER BY joined_at",
                user_key,
            )
            return [r["team_id"] for r in rows]
        except Exception:  # noqa: BLE001 — tables may not be bootstrapped yet
            logger.debug("teams_for(%s) failed (table missing?)", user_key, exc_info=True)
            return []

    async def orgs_for(self, user_key: str) -> list[str]:
        """Org ids the user belongs to — the distinct ``teams.org_id`` of every
        team they're a member of. This is the VERIFIABLE org boundary: a user
        is "in org X" iff they're a member of at least one team whose org_id is
        X, so an org-scoped connector can never be resolved cross-org."""
        if not user_key:
            return []
        try:
            rows = await self._pool.fetch(
                """SELECT DISTINCT t.org_id
                   FROM team_members m JOIN teams t ON t.id = m.team_id
                   WHERE m.user_uid = $1 AND COALESCE(t.org_id, '') <> ''""",
                user_key,
            )
            return [r["org_id"] for r in rows]
        except Exception:  # noqa: BLE001 — tables may not be bootstrapped yet
            logger.debug("orgs_for(%s) failed (table missing?)", user_key, exc_info=True)
            return []

    async def is_team_admin(self, team_id: str, user_key: str) -> bool:
        """True when the user is an ``admin`` member of the team. Team-scoped
        connector writes require this (not mere membership)."""
        if not (team_id and user_key):
            return False
        try:
            row = await self._pool.fetchrow(
                "SELECT roles FROM team_members WHERE team_id = $1 AND user_uid = $2",
                team_id,
                user_key,
            )
            return bool(row) and "admin" in (row["roles"] or [])
        except Exception:  # noqa: BLE001
            logger.debug("is_team_admin(%s,%s) failed", team_id, user_key, exc_info=True)
            return False

    async def is_org_admin(self, org_id: str, user_key: str) -> bool:
        """True when the user is an admin of ANY team in the org — the gate for
        org-scoped connector writes."""
        if not (org_id and user_key):
            return False
        try:
            row = await self._pool.fetchrow(
                """SELECT 1
                   FROM team_members m JOIN teams t ON t.id = m.team_id
                   WHERE m.user_uid = $1 AND t.org_id = $2 AND 'admin' = ANY(m.roles)
                   LIMIT 1""",
                user_key,
                org_id,
            )
            return bool(row)
        except Exception:  # noqa: BLE001
            logger.debug("is_org_admin(%s,%s) failed", org_id, user_key, exc_info=True)
            return False

    async def members(self, team_id: str) -> list[dict[str, Any]]:
        try:
            rows = await self._pool.fetch(
                "SELECT user_uid, roles, joined_at FROM team_members WHERE team_id = $1 ORDER BY joined_at",
                team_id,
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            logger.debug("members(%s) failed", team_id, exc_info=True)
            return []

    async def add_member(self, team_id: str, user_uid: str, roles: list[str] | None = None) -> bool:
        try:
            await self._pool.execute(
                """INSERT INTO team_members (team_id, user_uid, roles)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (team_id, user_uid) DO UPDATE SET roles = EXCLUDED.roles""",
                team_id,
                user_uid,
                list(roles or []),
            )
            return True
        except Exception:  # noqa: BLE001
            logger.warning("add_member(%s,%s) failed", team_id, user_uid, exc_info=True)
            return False

    # ── Teams ─────────────────────────────────────────────────────────

    async def list_teams(self) -> list[dict[str, Any]]:
        try:
            rows = await self._pool.fetch("SELECT * FROM teams ORDER BY created_at DESC")
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            logger.debug("list_teams failed", exc_info=True)
            return []

    async def get_team(self, team_id: str) -> dict[str, Any] | None:
        try:
            row = await self._pool.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    async def create_team(self, name: str, created_by: str, org_id: str = "") -> str:
        team_id = f"team-{uuid.uuid4().hex[:12]}"
        try:
            await self._pool.execute(
                "INSERT INTO teams (id, name, org_id, created_by) VALUES ($1, $2, $3, $4)",
                team_id,
                name,
                org_id,
                created_by,
            )
            # The creator is the first member, as an admin.
            await self.add_member(team_id, created_by, ["admin"])
            return team_id
        except Exception:  # noqa: BLE001
            logger.warning("create_team(%s) failed", name, exc_info=True)
            raise

    # ── Authorization ─────────────────────────────────────────────────

    def can_dispatch(self, principal: Principal | None, team_id: str) -> bool:
        """A dispatch into `team_id` is allowed when it's unscoped (no team)
        or the principal is a member. System/webhook principals (no team_ids
        resolved) are allowed through — they run unscoped by design."""
        if not team_id:
            return True
        if principal is None:
            return True  # synthetic (webhook/cron) — unscoped
        if not principal.team_ids:
            return True  # not a teams user — back-compat global access
        return team_id in principal.team_ids

    # ── Crews (Phase 3) ───────────────────────────────────────────────

    async def list_crews(self, team_id: str | None = None) -> list[dict[str, Any]]:
        try:
            if team_id:
                rows = await self._pool.fetch(
                    "SELECT * FROM agent_crews WHERE team_id = $1 ORDER BY created_at DESC", team_id
                )
            else:
                rows = await self._pool.fetch("SELECT * FROM agent_crews ORDER BY created_at DESC")
            return [_decode_crew(dict(r)) for r in rows]
        except Exception:  # noqa: BLE001
            logger.debug("list_crews failed", exc_info=True)
            return []

    async def get_crew(self, crew_id: str) -> dict[str, Any] | None:
        try:
            row = await self._pool.fetchrow("SELECT * FROM agent_crews WHERE id = $1", crew_id)
            return _decode_crew(dict(row)) if row else None
        except Exception:  # noqa: BLE001
            return None

    async def create_crew(
        self,
        team_id: str,
        name: str,
        members: list[dict[str, Any]],
        lead: str,
        description: str = "",
    ) -> str:
        import json

        crew_id = f"crew-{uuid.uuid4().hex[:12]}"
        try:
            await self._pool.execute(
                """INSERT INTO agent_crews (id, team_id, name, description, members, lead)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                crew_id,
                team_id,
                name,
                description,
                json.dumps(members),
                lead,
            )
            return crew_id
        except Exception:  # noqa: BLE001
            logger.warning("create_crew(%s) failed", name, exc_info=True)
            raise


def _decode_crew(row: dict[str, Any]) -> dict[str, Any]:
    """`members` is JSONB — asyncpg may hand it back as a str on some paths."""
    import json

    members = row.get("members")
    if isinstance(members, str):
        try:
            row["members"] = json.loads(members)
        except (json.JSONDecodeError, TypeError):
            row["members"] = []
    return row


__all__ = ["TeamService"]
