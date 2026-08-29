"""Rollup queries over `audit_log` for the admin overview.

Read-only aggregates only. `audit_log` is append-only by design (the
schema comment forbids UPDATE/DELETE), and it is already indexed on
`action` and `created_at DESC`, which is exactly the access pattern here.

Every function degrades to an empty result when the database is missing
or the query fails — the admin tab renders with blank sections rather
than erroring, matching the analytics page's existing contract.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.admin.activity import ACTION_ACTIVE, ACTION_LOGIN

logger = logging.getLogger(__name__)

_ACTIVE_TIMESERIES_SQL = """
    SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS date,
           COUNT(DISTINCT actor)                                AS users
      FROM audit_log
     WHERE action = $2
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
     GROUP BY 1
     ORDER BY 1
"""

_SIGNIN_COUNT_SQL = """
    SELECT COUNT(*) AS count
      FROM audit_log
     WHERE action = $2
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
"""

# Rows in audit_log are deduplicated by Redis SET NX EX guard: one user per day max.
_USER_TOTALS_SQL = """
    SELECT actor                                                     AS "user",
           COUNT(*)                                                  AS days_active,
           to_char(MAX(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS last_seen
      FROM audit_log
     WHERE action = $2
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
     GROUP BY actor
     ORDER BY days_active DESC
"""


async def _fetch(database: Any, sql: str, days: int, action: str) -> list[dict[str, Any]]:
    pool = getattr(database, "pool", None) if database is not None else None
    if pool is None:
        return []
    try:
        return [dict(row) for row in await pool.fetch(sql, int(days), action)]
    except Exception:  # noqa: BLE001
        logger.debug("admin: rollup query failed — degrading to empty", exc_info=True)
        return []


async def active_users_timeseries(database: Any, days: int) -> list[dict[str, Any]]:
    """Distinct active users per day, oldest first."""
    return await _fetch(database, _ACTIVE_TIMESERIES_SQL, days, ACTION_ACTIVE)


async def signin_count(database: Any, days: int) -> int:
    """Explicit sign-in events in the window (local-dev logins only)."""
    rows = await _fetch(database, _SIGNIN_COUNT_SQL, days, ACTION_LOGIN)
    return int(rows[0].get("count", 0)) if rows else 0


async def active_user_totals(database: Any, days: int) -> list[dict[str, Any]]:
    """Per-user active-day counts, busiest first."""
    return await _fetch(database, _USER_TOTALS_SQL, days, ACTION_ACTIVE)
