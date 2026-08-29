# Admin Analytics Tab + Trial Allowance UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give platform admins a per-user activity view inside `/analytics`, and give every user a visible free-token trial with onboarding demos and a bring-your-own-key prompt.

**Architecture:** A new `src/devai/admin/` package exposes `/api/admin/*` behind a router-level `require_admin` dependency; user activity is recorded into the existing append-only `audit_log` table (one row per user per day, Redis-deduplicated) and joined with the existing Redis `UsageLedger` for per-user call/token/cost rollups. OpenPanel is read through a server-side client that reports `enabled: false` until configured. The dashboard gains an admin-only tab on the existing analytics page and a trial provider that consumes the already-built `GET /api/settings/trial`.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, redis.asyncio, pytest; Next.js 15 (App Router), React, TypeScript, `node --test`.

**Spec:** `docs/superpowers/specs/2026-08-29-admin-analytics-trial-design.md`

## Global Constraints

- **No new SQL files.** CLAUDE.md §5 forbids `.sql` files/migrations in this repo. Reuse the existing `audit_log` table only. Raw SQL *inside* Python is fine and already the norm (`services/database.py:327`).
- **No container builds, image pushes, or deploys.** Verify with language tooling only: `pytest`, `ruff check src/`, `npm run build`, `tsc --noEmit`.
- **No changes to `tesserix-k8s`.** OpenPanel project onboarding is out of scope.
- **Do not enable strict mode.** `llm_require_user_connector` stays `False`.
- **No AI/Claude/Anthropic/Copilot references** in commits, comments, or file content. Conventional commit subjects, under 72 chars, no emoji.
- **Git identity:** `git config user.name "sam123ben"` and `git config user.email "samyak.rout@gmail.com"` (already set in this worktree).
- **Adapters rule (CLAUDE.md §6):** no vendor SDK at module top level. The OpenPanel client uses `httpx`, imported lazily inside the method, and never raises to the caller.
- **Every endpoint degrades** to empty/disabled rather than 5xx when a source is unavailable — the existing contract in `analytics/routes.py:14`.
- Admin roles are exactly `{"admin", "platform-admin"}`, matching `mcphub/server.py:69` and `evaluations/gates.py:133`.

---

### Task 1: Admin router with `require_admin` boundary

This is the authorization boundary the whole feature rests on, so it lands first and alone.

**Files:**
- Create: `src/devai/admin/__init__.py`
- Create: `src/devai/admin/routes.py`
- Test: `tests/unit/test_admin_routes.py`

**Interfaces:**
- Consumes: `devai.authz.require_principal(request) -> Principal` (existing, raises 401 when anonymous).
- Produces:
  - `devai.admin.routes.router` — `APIRouter(prefix="/api/admin", tags=["admin"])`
  - `devai.admin.routes.require_admin(request: Request) -> Principal` — raises `HTTPException(403)` for non-admins.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_routes.py`. This mirrors the fixture style of the existing `tests/unit/test_analytics_usage_routes.py` (both `identity` and `authz` are patched because `require_principal` calls through `authz`'s imported reference).

```python
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.authz as authz
import devai.identity as identity
from devai.admin.routes import router
from devai.identity import Principal


def _client(principal: Principal | None, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.usage_ledger = None
    app.state.analytics_db = None
    app.state.config = None

    async def _extract(_request):
        return principal

    monkeypatch.setattr(identity, "extract_principal", _extract)
    monkeypatch.setattr(authz, "extract_principal", _extract)
    return TestClient(app)


def _admin() -> Principal:
    return Principal(email="samyak.rout@gmail.com", uid="u-admin", roles=["admin"])


def _plain() -> Principal:
    return Principal(email="someone@example.com", uid="u-plain", roles=[])


# Every admin route must be listed here. A new route added without a test
# entry is caught by test_every_admin_route_is_covered below.
ADMIN_ROUTES = ["/api/admin/overview"]


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_non_admin_is_forbidden(path, monkeypatch):
    res = _client(_plain(), monkeypatch).get(path)
    assert res.status_code == 403


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_anonymous_is_unauthorized(path, monkeypatch):
    res = _client(None, monkeypatch).get(path)
    assert res.status_code == 401


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_admin_is_allowed(path, monkeypatch):
    res = _client(_admin(), monkeypatch).get(path)
    assert res.status_code == 200


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_platform_admin_is_allowed(path, monkeypatch):
    principal = Principal(email="p@example.com", uid="u-p", roles=["platform-admin"])
    res = _client(principal, monkeypatch).get(path)
    assert res.status_code == 200


def test_every_admin_route_is_covered():
    """A new /api/admin route must be added to ADMIN_ROUTES, so it inherits
    the 401/403 assertions above rather than shipping unguarded."""
    declared = {r.path for r in router.routes}
    assert declared == set(ADMIN_ROUTES)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'devai.admin'`

- [ ] **Step 3: Create the package**

Create `src/devai/admin/__init__.py`:

```python
"""Platform-owner surface — /api/admin/*.

Every route here is gated by :func:`devai.admin.routes.require_admin`, which
is applied to the router itself rather than per handler, so a route added
later cannot ship unguarded.
"""

from devai.admin.routes import router

__all__ = ["router"]
```

- [ ] **Step 4: Write the router with the guard**

Create `src/devai/admin/routes.py`:

```python
"""Admin-only analytics endpoints.

Mounted at `/api/admin/*` by `devai.webhook.app.create_app`. Read-only.

The guard is a router-level dependency: FastAPI resolves it before any
handler in this module runs, so authorization is stated once instead of
being repeated (and eventually forgotten) per endpoint. The dashboard's
admin tab renders only when these endpoints answer 200 — the API is the
authority, never a client-side email check.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

if TYPE_CHECKING:
    from fastapi import Request

    from devai.identity import Principal

logger = logging.getLogger(__name__)

_ADMIN_ROLES = frozenset({"admin", "platform-admin"})


async def require_admin(request: Request) -> Principal:
    """Resolve the caller and require an admin role, else 403.

    Anonymous callers get 401 from ``require_principal`` first — the two
    codes stay distinct so the dashboard can tell "signed out" from
    "signed in but not an owner".
    """
    from devai.authz import require_principal

    principal = await require_principal(request)
    roles = set(getattr(principal, "roles", None) or [])
    if not (_ADMIN_ROLES & roles):
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """Platform activity: active users, sign-ins, and per-user LLM usage."""
    return {"active_users": [], "signins": 0, "by_user": [], "enabled": False}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_routes.py -v`
Expected: PASS (13 tests)

- [ ] **Step 6: Lint**

Run: `ruff check src/devai/admin/`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add src/devai/admin tests/unit/test_admin_routes.py
git commit -m "feat(admin): add admin-only router with role guard"
```

---

### Task 2: Daily active-user recording

**Files:**
- Create: `src/devai/admin/activity.py`
- Test: `tests/unit/test_admin_activity.py`

**Interfaces:**
- Consumes: `devai.services.database.Database.audit(action, actor, actor_type=..., details=...)` (existing, `services/database.py:315`).
- Produces:
  - `devai.admin.activity.ACTION_ACTIVE = "user_active"`
  - `devai.admin.activity.ACTION_LOGIN = "login"`
  - `async devai.admin.activity.record_active(app_state, principal) -> bool` — `True` when a row was written, `False` when deduplicated or unavailable.
  - `devai.admin.activity.ActivityMiddleware` — Starlette `BaseHTTPMiddleware` subclass.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_activity.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.activity import ACTION_ACTIVE, record_active
from devai.identity import Principal


class _Database:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def audit(self, action, actor, actor_type="agent", details=None, **_kw):
        self.rows.append({"action": action, "actor": actor, "actor_type": actor_type, "details": details})


class _Redis:
    """Minimal SET NX EX stand-in — the real dedup guard."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


class _BrokenRedis:
    async def set(self, *_a, **_kw):
        raise RuntimeError("redis down")


def _state(db, redis):
    return SimpleNamespace(analytics_db=db, activity_redis=redis)


def _principal() -> Principal:
    return Principal(email="user@example.com", uid="u-1", tenant_id="t-1")


@pytest.mark.asyncio
async def test_records_one_row_for_a_user():
    db, state = _Database(), None
    state = _state(db, _Redis())
    assert await record_active(state, _principal()) is True
    assert len(db.rows) == 1
    assert db.rows[0]["action"] == ACTION_ACTIVE
    assert db.rows[0]["actor"] == "user@example.com"
    assert db.rows[0]["actor_type"] == "user"


@pytest.mark.asyncio
async def test_second_request_same_day_is_deduplicated():
    db = _Database()
    state = _state(db, _Redis())
    assert await record_active(state, _principal()) is True
    assert await record_active(state, _principal()) is False
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_distinct_users_each_get_a_row():
    db = _Database()
    state = _state(db, _Redis())
    await record_active(state, Principal(email="a@example.com", uid="a"))
    await record_active(state, Principal(email="b@example.com", uid="b"))
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_redis_failure_does_not_raise():
    db = _Database()
    state = _state(db, _BrokenRedis())
    assert await record_active(state, _principal()) is False
    assert db.rows == []


@pytest.mark.asyncio
async def test_missing_database_does_not_raise():
    state = _state(None, _Redis())
    assert await record_active(state, _principal()) is False


@pytest.mark.asyncio
async def test_anonymous_principal_is_ignored():
    db = _Database()
    state = _state(db, _Redis())
    assert await record_active(state, None) is False
    assert db.rows == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'devai.admin.activity'`

- [ ] **Step 3: Implement the recorder**

Create `src/devai/admin/activity.py`:

```python
"""User-activity recording for the admin overview.

DevAI never observes a login in production: auth-bff terminates OAuth
outside the pod and forwards `X-Forwarded-*` identity headers, so the
backend sees authenticated *requests*, not sign-in *moments*. What it can
count exactly is therefore DAILY ACTIVE USERS — distinct principals that
made at least one request on a given day. That's what the admin page
labels it, rather than presenting it as a login count.

Rows land in the existing append-only `audit_log` table (no new schema —
CLAUDE.md forbids SQL here). A Redis `SET NX EX` guard collapses a user's
whole day to a single row, so this costs one write per user per day, not
one per request, and holds across pods.

Recording is best-effort in the strictest sense: every failure path
returns False and writes nothing. A telemetry miss must never fail a
user's request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from devai.identity import Principal

logger = logging.getLogger(__name__)

ACTION_ACTIVE = "user_active"
ACTION_LOGIN = "login"

_DEDUP_KEY = "devai:activity:{day}:{actor}"
_DEDUP_TTL_SECONDS = 48 * 60 * 60  # outlives the day it guards, then reaps itself


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def record_active(app_state: Any, principal: Principal | None) -> bool:
    """Record one `user_active` row for this principal, once per day."""
    actor = getattr(principal, "email", "") or getattr(principal, "uid", "")
    if not actor or not isinstance(actor, str):
        return False
    # Synthetic principals are machines, not users on a dashboard.
    if actor.startswith(("webhook:", "system:")) or actor in {"system@devai", "webhook@devai"}:
        return False

    database = getattr(app_state, "analytics_db", None)
    redis = getattr(app_state, "activity_redis", None)
    if database is None or redis is None:
        return False

    day = _today()
    try:
        claimed = await redis.set(
            _DEDUP_KEY.format(day=day, actor=actor.lower()),
            "1",
            nx=True,
            ex=_DEDUP_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: dedup guard unavailable — skipping row", exc_info=True)
        return False
    if not claimed:
        return False

    try:
        await database.audit(
            action=ACTION_ACTIVE,
            actor=actor,
            actor_type="user",
            details={
                "day": day,
                "uid": getattr(principal, "uid", "") or "",
                "tenant_id": getattr(principal, "tenant_id", "") or "",
                "auth_provider": getattr(principal, "auth_provider", "") or "",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: audit write failed", exc_info=True)
        return False
    return True


class ActivityMiddleware(BaseHTTPMiddleware):
    """Record the caller as active for today, then get out of the way."""

    # Probes and static assets say nothing about a human being present.
    _SKIP_PREFIXES = ("/healthz", "/readyz", "/webhook/", "/metrics")

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in self._SKIP_PREFIXES):
            return response
        try:
            from devai.identity import extract_principal

            principal = await extract_principal(request)
            await record_active(request.app.state, principal)
        except Exception:  # noqa: BLE001
            logger.debug("activity middleware: recording skipped", exc_info=True)
        return response
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_activity.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/devai/admin/
git add src/devai/admin/activity.py tests/unit/test_admin_activity.py
git commit -m "feat(admin): record daily active users into audit_log"
```

---

### Task 3: Activity rollup queries

**Files:**
- Create: `src/devai/admin/service.py`
- Test: `tests/unit/test_admin_service.py`

**Interfaces:**
- Consumes: `ACTION_ACTIVE`, `ACTION_LOGIN` from Task 2; a `Database` exposing `pool.fetch(sql, *args)` (asyncpg, as `services/database.py` uses).
- Produces:
  - `async devai.admin.service.active_users_timeseries(database, days: int) -> list[dict]` — `[{"date": "YYYY-MM-DD", "users": int}]`, oldest first.
  - `async devai.admin.service.signin_count(database, days: int) -> int`
  - `async devai.admin.service.active_user_totals(database, days: int) -> list[dict]` — `[{"user": str, "days_active": int, "last_seen": str}]`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_service.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.service import active_user_totals, active_users_timeseries, signin_count


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[tuple] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows


class _BrokenPool:
    async def fetch(self, *_a):
        raise RuntimeError("db down")


def _db(pool):
    return SimpleNamespace(pool=pool)


@pytest.mark.asyncio
async def test_timeseries_maps_rows():
    pool = _Pool([{"date": "2026-08-27", "users": 3}, {"date": "2026-08-28", "users": 5}])
    out = await active_users_timeseries(_db(pool), 30)
    assert out == [{"date": "2026-08-27", "users": 3}, {"date": "2026-08-28", "users": 5}]


@pytest.mark.asyncio
async def test_timeseries_passes_the_day_window():
    pool = _Pool([])
    await active_users_timeseries(_db(pool), 7)
    assert pool.queries[0][1] == (7,)


@pytest.mark.asyncio
async def test_signin_count_reads_the_scalar():
    pool = _Pool([{"count": 12}])
    assert await signin_count(_db(pool), 30) == 12


@pytest.mark.asyncio
async def test_signin_count_with_no_rows_is_zero():
    assert await signin_count(_db(_Pool([])), 30) == 0


@pytest.mark.asyncio
async def test_user_totals_maps_rows():
    pool = _Pool([{"user": "a@example.com", "days_active": 4, "last_seen": "2026-08-29"}])
    out = await active_user_totals(_db(pool), 30)
    assert out == [{"user": "a@example.com", "days_active": 4, "last_seen": "2026-08-29"}]


@pytest.mark.asyncio
async def test_missing_database_degrades_to_empty():
    assert await active_users_timeseries(None, 30) == []
    assert await signin_count(None, 30) == 0
    assert await active_user_totals(None, 30) == []


@pytest.mark.asyncio
async def test_query_failure_degrades_to_empty():
    db = _db(_BrokenPool())
    assert await active_users_timeseries(db, 30) == []
    assert await signin_count(db, 30) == 0
    assert await active_user_totals(db, 30) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'devai.admin.service'`

- [ ] **Step 3: Implement the rollups**

Create `src/devai/admin/service.py`:

```python
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

logger = logging.getLogger(__name__)

_ACTIVE_TIMESERIES_SQL = """
    SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS date,
           COUNT(DISTINCT actor)                                AS users
      FROM audit_log
     WHERE action = 'user_active'
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
     GROUP BY 1
     ORDER BY 1
"""

_SIGNIN_COUNT_SQL = """
    SELECT COUNT(*) AS count
      FROM audit_log
     WHERE action = 'login'
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
"""

_USER_TOTALS_SQL = """
    SELECT actor                                                     AS "user",
           COUNT(*)                                                  AS days_active,
           to_char(MAX(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS last_seen
      FROM audit_log
     WHERE action = 'user_active'
       AND created_at >= NOW() - ($1::int * INTERVAL '1 day')
     GROUP BY actor
     ORDER BY days_active DESC
"""


async def _fetch(database: Any, sql: str, days: int) -> list[dict[str, Any]]:
    pool = getattr(database, "pool", None) if database is not None else None
    if pool is None:
        return []
    try:
        return [dict(row) for row in await pool.fetch(sql, int(days))]
    except Exception:  # noqa: BLE001
        logger.debug("admin: rollup query failed — degrading to empty", exc_info=True)
        return []


async def active_users_timeseries(database: Any, days: int) -> list[dict[str, Any]]:
    """Distinct active users per day, oldest first."""
    return await _fetch(database, _ACTIVE_TIMESERIES_SQL, days)


async def signin_count(database: Any, days: int) -> int:
    """Explicit sign-in events in the window (local-dev logins only)."""
    rows = await _fetch(database, _SIGNIN_COUNT_SQL, days)
    return int(rows[0].get("count", 0)) if rows else 0


async def active_user_totals(database: Any, days: int) -> list[dict[str, Any]]:
    """Per-user active-day counts, busiest first."""
    return await _fetch(database, _USER_TOTALS_SQL, days)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_service.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/devai/admin/
git add src/devai/admin/service.py tests/unit/test_admin_service.py
git commit -m "feat(admin): add active-user rollup queries"
```

---

### Task 4: OpenPanel client and config

**Files:**
- Create: `src/devai/admin/openpanel.py`
- Modify: `src/devai/config.py` (add a settings block near the other integration blocks)
- Test: `tests/unit/test_admin_openpanel.py`

**Interfaces:**
- Consumes: `Settings` attributes added in this task.
- Produces:
  - `async devai.admin.openpanel.fetch_overview(config, days: int) -> dict` — always returns a dict containing `enabled: bool`; never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_openpanel.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.openpanel import fetch_overview


def _config(**kw):
    base = {
        "openpanel_api_url": "https://analytics.example.com/api",
        "openpanel_client_id": "cid",
        "openpanel_client_secret": "secret",
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_disabled_without_api_url():
    out = await fetch_overview(_config(openpanel_api_url=""), 30)
    assert out == {"enabled": False, "reason": "not configured"}


@pytest.mark.asyncio
async def test_disabled_without_client_id():
    out = await fetch_overview(_config(openpanel_client_id=""), 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_disabled_without_secret():
    out = await fetch_overview(_config(openpanel_client_secret=""), 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_disabled_with_no_config_object():
    out = await fetch_overview(None, 30)
    assert out["enabled"] is False


@pytest.mark.asyncio
async def test_returns_payload_when_configured(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"visitors": 42, "sessions": 60}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await fetch_overview(_config(), 7)
    assert out["enabled"] is True
    assert out["visitors"] == 42
    assert captured["params"]["days"] == 7
    # The secret authenticates server-side and must never reach the browser.
    assert captured["headers"]["openpanel-client-secret"] == "secret"


@pytest.mark.asyncio
async def test_upstream_failure_degrades_to_disabled(monkeypatch):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_kw):
            raise RuntimeError("unreachable")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await fetch_overview(_config(), 30)
    assert out["enabled"] is False
    assert "unavailable" in out["reason"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_openpanel.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'devai.admin.openpanel'`

- [ ] **Step 3: Add the settings block**

In `src/devai/config.py`, add this block immediately after the `admin_emails` field (around line 297), keeping the existing comment style:

```python
    # --- openpanel (web analytics) ---
    # OpenPanel runs in-cluster (tesserix-k8s charts/thirdparty/openpanel) and
    # answers page-level questions the backend can't: hits, sessions,
    # referrers. Read server-side only so the client secret never reaches a
    # browser. Any of the three unset leaves /api/admin/openpanel reporting
    # {"enabled": false} — DevAI is not onboarded as an OpenPanel project yet.
    openpanel_api_url: str = ""  # e.g. https://analytics.tesserix.app/api
    openpanel_client_id: str = ""
    openpanel_client_secret: str = ""
```

- [ ] **Step 4: Implement the client**

Create `src/devai/admin/openpanel.py`:

```python
"""Server-side OpenPanel reader for the admin overview.

OpenPanel is the ClickHouse-backed web analytics already deployed in the
cluster. It answers what the backend cannot — page hits, sessions,
referrers — but it is client-side instrumented, so its numbers are
approximate (ad-blockers undercount) and the page labels them as such.

Two deliberate properties:
  - The client secret is used here and never sent to the browser; the
    dashboard reaches OpenPanel only through this proxy.
  - Nothing raises. Unconfigured or unreachable both return
    {"enabled": False, ...}, so this ships and passes its tests before
    DevAI is onboarded as an OpenPanel project in tesserix-k8s.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


async def fetch_overview(config: Any, days: int) -> dict[str, Any]:
    """Visitor/session rollup from OpenPanel, or a disabled marker."""
    api_url = (getattr(config, "openpanel_api_url", "") or "").rstrip("/")
    client_id = getattr(config, "openpanel_client_id", "") or ""
    client_secret = getattr(config, "openpanel_client_secret", "") or ""
    if not (api_url and client_id and client_secret):
        return {"enabled": False, "reason": "not configured"}

    try:
        import httpx  # noqa: PLC0415 — lazy per the adapter convention

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            res = await client.get(
                f"{api_url}/export/overview",
                params={"days": int(days)},
                headers={
                    "openpanel-client-id": client_id,
                    "openpanel-client-secret": client_secret,
                },
            )
            res.raise_for_status()
            payload = res.json()
    except Exception:  # noqa: BLE001
        logger.info("admin: OpenPanel unreachable — section degrades to empty", exc_info=True)
        return {"enabled": False, "reason": "unavailable"}

    if not isinstance(payload, dict):
        return {"enabled": False, "reason": "unavailable"}
    return {"enabled": True, **payload}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_openpanel.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Lint and commit**

```bash
ruff check src/devai/admin/ src/devai/config.py
git add src/devai/admin/openpanel.py src/devai/config.py tests/unit/test_admin_openpanel.py
git commit -m "feat(admin): add server-side openpanel reader"
```

---

### Task 5: Wire the overview endpoint to real data

**Files:**
- Modify: `src/devai/admin/routes.py` (replace the Task 1 stub body; add the OpenPanel route)
- Modify: `tests/unit/test_admin_routes.py` (extend `ADMIN_ROUTES`, add payload assertions)

**Interfaces:**
- Consumes: `active_users_timeseries`, `signin_count`, `active_user_totals` (Task 3); `fetch_overview` (Task 4); `UsageLedger.by_user(tenant="")` (existing, returns rows with `user`, `user_id`, `tenant_id`, `cost_usd`).
- Produces:
  - `GET /api/admin/overview?days=N` → `{"active_users": [...], "signins": int, "by_user": [...], "user_activity": [...], "days": int, "enabled": bool}`
  - `GET /api/admin/openpanel?days=N` → OpenPanel payload or `{"enabled": false, ...}`

- [ ] **Step 1: Update the test**

In `tests/unit/test_admin_routes.py`, change the `ADMIN_ROUTES` constant:

```python
ADMIN_ROUTES = ["/api/admin/overview", "/api/admin/openpanel"]
```

Then append these tests to the same file:

```python
class _Ledger:
    async def by_user(self, tenant: str = ""):
        return [{"user": "a@example.com", "user_id": "a", "tenant_id": tenant, "cost_usd": 1.5, "calls": 9}]


def _wired_client(monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.usage_ledger = _Ledger()
    app.state.analytics_db = None
    app.state.config = None

    async def _extract(_request):
        return _admin()

    monkeypatch.setattr(identity, "extract_principal", _extract)
    monkeypatch.setattr(authz, "extract_principal", _extract)

    import devai.admin.routes as admin_routes

    async def _timeseries(_db, days):
        return [{"date": "2026-08-29", "users": 2}]

    async def _signins(_db, days):
        return 4

    async def _totals(_db, days):
        return [{"user": "a@example.com", "days_active": 3, "last_seen": "2026-08-29"}]

    monkeypatch.setattr(admin_routes, "active_users_timeseries", _timeseries)
    monkeypatch.setattr(admin_routes, "signin_count", _signins)
    monkeypatch.setattr(admin_routes, "active_user_totals", _totals)
    return TestClient(app)


def test_overview_returns_all_sections(monkeypatch):
    res = _wired_client(monkeypatch).get("/api/admin/overview?days=7")
    assert res.status_code == 200
    body = res.json()
    assert body["days"] == 7
    assert body["active_users"] == [{"date": "2026-08-29", "users": 2}]
    assert body["signins"] == 4
    assert body["user_activity"][0]["user"] == "a@example.com"
    assert body["by_user"][0]["cost_usd"] == 1.5


def test_overview_without_a_ledger_still_returns_200(monkeypatch):
    client = _wired_client(monkeypatch)
    client.app.state.usage_ledger = None
    res = client.get("/api/admin/overview")
    assert res.status_code == 200
    assert res.json()["by_user"] == []


def test_openpanel_reports_disabled_when_unconfigured(monkeypatch):
    res = _wired_client(monkeypatch).get("/api/admin/openpanel")
    assert res.status_code == 200
    assert res.json()["enabled"] is False


def test_days_query_is_bounded(monkeypatch):
    assert _wired_client(monkeypatch).get("/api/admin/overview?days=0").status_code == 422
    assert _wired_client(monkeypatch).get("/api/admin/overview?days=400").status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_admin_routes.py -v`
Expected: FAIL — `test_every_admin_route_is_covered` fails (`/api/admin/openpanel` not declared) and the new payload tests fail on missing keys.

- [ ] **Step 3: Implement the endpoints**

In `src/devai/admin/routes.py`, add to the imports:

```python
from fastapi import APIRouter, Depends, HTTPException, Query

from devai.admin.openpanel import fetch_overview
from devai.admin.service import active_user_totals, active_users_timeseries, signin_count
```

Then replace the stub `overview` handler with:

```python
async def _db(request: Request):
    """The analytics Postgres handle, or None when unreachable."""
    from devai.analytics.routes import _db as analytics_db

    return await analytics_db(request)


@router.get("/overview")
async def overview(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Platform activity: active users, sign-ins, and per-user LLM usage.

    Two different sources, deliberately kept distinct in the payload:
    `active_users`/`user_activity` are exact (audit_log), while `by_user`
    carries real spend from the Redis usage ledger.
    """
    database = await _db(request)
    ledger = getattr(request.app.state, "usage_ledger", None)

    by_user: list[dict[str, Any]] = []
    if ledger is not None:
        try:
            by_user = await ledger.by_user("")
        except Exception:  # noqa: BLE001
            logger.debug("admin: ledger by_user failed", exc_info=True)

    return {
        "days": days,
        "active_users": await active_users_timeseries(database, days),
        "signins": await signin_count(database, days),
        "user_activity": await active_user_totals(database, days),
        "by_user": by_user,
        "enabled": True,
    }


@router.get("/openpanel")
async def openpanel(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Page-level browsing stats. Reports disabled until configured."""
    return await fetch_overview(getattr(request.app.state, "config", None), days)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_routes.py -v`
Expected: PASS (all, including the 401/403 matrix now covering both routes)

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/devai/admin/
git add src/devai/admin/routes.py tests/unit/test_admin_routes.py
git commit -m "feat(admin): serve active-user and usage rollups"
```

---

### Task 6: Mount the router and middleware in the app

**Files:**
- Modify: `src/devai/webhook/app.py` (router registration near line 873; middleware + Redis handle near line 719)
- Test: `tests/unit/test_admin_app_wiring.py`

**Interfaces:**
- Consumes: `devai.admin.routes.router` (Task 5), `devai.admin.activity.ActivityMiddleware` (Task 2).
- Produces: `app.state.activity_redis` — a `redis.asyncio` client or `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_app_wiring.py`:

```python
from __future__ import annotations

from devai.admin.activity import ActivityMiddleware


def test_admin_router_is_registered():
    from devai.webhook.app import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/api/admin/overview" in paths
    assert "/api/admin/openpanel" in paths


def test_activity_middleware_is_installed():
    from devai.webhook.app import create_app

    app = create_app()
    assert any(m.cls is ActivityMiddleware for m in app.user_middleware)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_app_wiring.py -v`
Expected: FAIL — `/api/admin/overview` not in paths.

- [ ] **Step 3: Register the router**

In `src/devai/webhook/app.py`, directly after the analytics router registration (line 873, `app.include_router(analytics_router)`), add:

```python
    # Admin routes (/api/admin/*) — platform-owner view of who uses DevAI.
    # Gated by a router-level admin-role dependency, not by the edge, so the
    # boundary holds regardless of how the pod is reached.
    from devai.admin.routes import router as admin_router

    app.include_router(admin_router)
```

- [ ] **Step 4: Install the middleware and Redis handle**

In `src/devai/webhook/app.py`, directly after `app.add_middleware(BodySizeLimitMiddleware)` (line 719), add:

```python
    # Daily active-user recording. Added here so it sits inside the auth gate
    # and sees the resolved request; failures inside it are swallowed, so a
    # telemetry miss can never fail a user request.
    from devai.admin.activity import ActivityMiddleware

    app.add_middleware(ActivityMiddleware)

    # Dedup guard for the above — one audit_log row per user per day, shared
    # across pods. None degrades to "record nothing", never to a crash.
    app.state.activity_redis = None
    try:
        import redis.asyncio as _redis

        url = getattr(config, "redis_url", "") or ""
        if url:
            app.state.activity_redis = _redis.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001
        logger.info("activity dedup guard unavailable — active-user stats disabled")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_app_wiring.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full backend suite for regressions**

Run: `python -m pytest tests/unit -q`
Expected: PASS. The new middleware runs on every request in every route test, so this step is the real check that it is genuinely non-fatal.

- [ ] **Step 7: Lint and commit**

```bash
ruff check src/devai/
git add src/devai/webhook/app.py tests/unit/test_admin_app_wiring.py
git commit -m "feat(admin): mount admin routes and activity middleware"
```

---

### Task 7: Record local sign-ins

**Files:**
- Modify: `src/devai/dashboard/local_auth_routes.py:49-91` (the `auth_login` handler)
- Test: `tests/unit/test_admin_activity.py` (append)

**Interfaces:**
- Consumes: `ACTION_LOGIN` from Task 2.
- Produces: an `audit_log` row with `action="login"`, `actor_type="user"` on each successful local login.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_admin_activity.py`:

```python
@pytest.mark.asyncio
async def test_record_login_writes_a_row():
    from devai.admin.activity import ACTION_LOGIN, record_login

    db = _Database()
    state = SimpleNamespace(analytics_db=db)
    assert await record_login(state, "user@example.com") is True
    assert db.rows[0]["action"] == ACTION_LOGIN
    assert db.rows[0]["actor_type"] == "user"


@pytest.mark.asyncio
async def test_record_login_without_database_is_silent():
    from devai.admin.activity import record_login

    assert await record_login(SimpleNamespace(analytics_db=None), "u@example.com") is False


@pytest.mark.asyncio
async def test_record_login_is_not_deduplicated():
    """Unlike active-days, every sign-in is its own event."""
    from devai.admin.activity import record_login

    db = _Database()
    state = SimpleNamespace(analytics_db=db)
    await record_login(state, "user@example.com")
    await record_login(state, "user@example.com")
    assert len(db.rows) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_admin_activity.py -v`
Expected: FAIL — `ImportError: cannot import name 'record_login'`

- [ ] **Step 3: Add the recorder**

Append to `src/devai/admin/activity.py`:

```python
async def record_login(app_state: Any, actor: str) -> bool:
    """Record an explicit sign-in. Local-dev only — in production auth-bff
    terminates OAuth outside this pod, so no login reaches us and the admin
    page sources sign-ins from OpenPanel instead."""
    if not actor:
        return False
    database = getattr(app_state, "analytics_db", None)
    if database is None:
        return False
    try:
        await database.audit(
            action=ACTION_LOGIN,
            actor=actor,
            actor_type="user",
            details={"day": _today(), "source": "local"},
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: login audit failed", exc_info=True)
        return False
    return True
```

- [ ] **Step 4: Call it from the login handler**

In `src/devai/dashboard/local_auth_routes.py`, inside `auth_login`, immediately before the successful return, add:

```python
    # Best-effort sign-in record for the admin overview.
    from devai.admin.activity import record_login

    await record_login(request.app.state, body.username)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_admin_activity.py tests/unit/test_local_auth_routes.py -v`
(If `tests/unit/test_local_auth_routes.py` does not exist, run only the first.)
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
ruff check src/devai/
git add src/devai/admin/activity.py src/devai/dashboard/local_auth_routes.py tests/unit/test_admin_activity.py
git commit -m "feat(admin): record local sign-in events"
```

---

### Task 8: Dashboard API client for admin + trial

**Files:**
- Modify: `dashboard/src/lib/api.ts` (types near line 1651; methods near line 1159)
- Test: `dashboard/src/lib/admin-api.test.ts`

**Interfaces:**
- Produces:
  - `AdminOverview`, `AdminOpenPanel`, `TrialStatus` interfaces
  - `api.admin.overview(days?)`, `api.admin.openpanel(days?)`
  - `api.getTrialStatus()` — already exists; only its return type is widened

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/lib/admin-api.test.ts`:

```ts
import test from "node:test";
import assert from "node:assert/strict";

import { adminOverviewPath, adminOpenPanelPath, trialTone } from "./admin-api";

test("overview path carries the day window", () => {
  assert.equal(adminOverviewPath(7), "/admin/overview?days=7");
});

test("openpanel path carries the day window", () => {
  assert.equal(adminOpenPanelPath(30), "/admin/openpanel?days=30");
});

test("trial tone is ok below the warning threshold", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 10, remaining: 90, exhausted: false, warning: false }), "ok");
});

test("trial tone warns at the 80 percent mark", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 80, remaining: 20, exhausted: false, warning: true }), "warning");
});

test("trial tone is exhausted when the budget is spent", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 100, remaining: 0, exhausted: true, warning: true }), "exhausted");
});

test("trial tone is hidden when trials are disabled", () => {
  assert.equal(trialTone({ trial_enabled: false, budget: 0, used: 0, remaining: 0, exhausted: false, warning: false }), "hidden");
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && npx tsx --test src/lib/admin-api.test.ts` (or `npm test`)
Expected: FAIL — cannot find module `./admin-api`

- [ ] **Step 3: Create the pure helpers**

Create `dashboard/src/lib/admin-api.ts`:

```ts
/**
 * Path builders and the trial tone rule, kept free of `fetch` so they are
 * directly unit-testable. `api.admin.*` in api.ts consumes these.
 */

export interface TrialStatus {
  trial_enabled: boolean;
  budget: number;
  used: number;
  remaining: number;
  exhausted: boolean;
  warning: boolean;
  has_own_connector?: boolean;
  applicable?: boolean;
}

export type TrialTone = "hidden" | "ok" | "warning" | "exhausted";

export function adminOverviewPath(days = 30): string {
  return `/admin/overview?days=${days}`;
}

export function adminOpenPanelPath(days = 30): string {
  return `/admin/openpanel?days=${days}`;
}

/**
 * Which trial treatment to show. The backend already computes `warning`
 * at >=80% and `exhausted`, so this maps rather than re-derives — one
 * threshold, defined server-side in settings/trial.py.
 */
export function trialTone(status: TrialStatus | null | undefined): TrialTone {
  if (!status || !status.trial_enabled) return "hidden";
  if (status.exhausted) return "exhausted";
  if (status.warning) return "warning";
  return "ok";
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && npx tsx --test src/lib/admin-api.test.ts`
Expected: PASS (6 tests)

- [ ] **Step 5: Add the typed API methods**

In `dashboard/src/lib/api.ts`, add near the other analytics types (after line 1651's section header):

```ts
// ── Admin (GET /api/admin/*) ───────────────────────────────────────────
export interface AdminActiveUserPoint {
  date: string;
  users: number;
}

export interface AdminUserActivity {
  user: string;
  days_active: number;
  last_seen: string;
}

export interface AdminUserUsage {
  user: string;
  user_id: string;
  tenant_id: string;
  cost_usd: number;
  calls?: number;
  tokens_in?: number;
  tokens_out?: number;
}

export interface AdminOverview {
  days: number;
  active_users: AdminActiveUserPoint[];
  signins: number;
  user_activity: AdminUserActivity[];
  by_user: AdminUserUsage[];
  enabled: boolean;
}

export interface AdminOpenPanel {
  enabled: boolean;
  reason?: string;
  visitors?: number;
  sessions?: number;
  pageviews?: number;
}
```

And add these method groups alongside `api.analytics` (after line 1159):

```ts
  // ── Admin: platform-owner view. 403 for non-admins by design — the
  // caller treats that as "hide the tab", not as an error to surface.
  admin: {
    overview: (days = 30) => apiFetch<AdminOverview>(adminOverviewPath(days), { soft: true }),
    openpanel: (days = 30) => apiFetch<AdminOpenPanel>(adminOpenPanelPath(days), { soft: true }),
  },
```

**Do not add a trial method.** `api.getTrialStatus()` already exists at `dashboard/src/lib/api.ts:535`, already calls `/settings/trial` with `{ soft: true }`, and is currently unused — it was written for exactly this banner. Task 10 consumes it as-is. Only widen its return type to the shared interface so the tone helper and the banner agree on one type:

```ts
  getTrialStatus: () => apiFetch<TrialStatus>("/settings/trial", { soft: true }),
```

Add the import at the top of `api.ts`:

```ts
import { adminOpenPanelPath, adminOverviewPath, type TrialStatus } from "./admin-api";
export type { TrialStatus } from "./admin-api";
```

Note that `TrialStatus` in `admin-api.ts` must keep `has_own_connector` and `applicable` as optional, since the existing inline type declares them required and the endpoint always returns them.

- [ ] **Step 6: Typecheck and commit**

```bash
cd dashboard && npx tsc --noEmit && npm run build
git add dashboard/src/lib/admin-api.ts dashboard/src/lib/admin-api.test.ts dashboard/src/lib/api.ts
git commit -m "feat(dashboard): add admin and trial api clients"
```

---

### Task 9: Admin tab on the analytics page

**Files:**
- Create: `dashboard/src/components/admin-panel.tsx`
- Modify: `dashboard/src/app/analytics/page.tsx` (TABS at line 40-45; tab render blocks after line 560)

**Interfaces:**
- Consumes: `api.admin.overview`, `api.admin.openpanel`, `AdminOverview`, `AdminOpenPanel` (Task 8); `Donut`, `HBarChart`, `LineChart` from `@/components/charts`.
- Produces: `<AdminPanel />`, default export absent — named export only, matching the other components in that directory.

- [ ] **Step 1: Create the panel component**

Create `dashboard/src/components/admin-panel.tsx`:

```tsx
"use client";

/**
 * Admin-only platform view — who uses DevAI.
 *
 * Visibility is decided by the API, not the client: /api/admin/overview
 * answers 403 for non-admins and the caller renders nothing. No email or
 * role is checked here, so the tab cannot be revealed by editing state.
 *
 * Two sources with different exactness, labelled as such:
 *   - active users / per-user usage — exact, from audit_log + usage ledger
 *   - visitors / sessions — approximate, client-reported via OpenPanel
 */

import { useEffect, useState } from "react";
import { Users, MousePointerClick, LogIn } from "lucide-react";

import { api, type AdminOpenPanel, type AdminOverview } from "@/lib/api";
import { HBarChart, LineChart } from "@/components/charts";

export function AdminPanel({ days }: { days: number }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [panel, setPanel] = useState<AdminOpenPanel | null>(null);

  useEffect(() => {
    let live = true;
    api.admin
      .overview(days)
      .then((d) => live && setOverview(d))
      .catch(() => live && setOverview(null));
    api.admin
      .openpanel(days)
      .then((d) => live && setPanel(d))
      .catch(() => live && setPanel({ enabled: false, reason: "unavailable" }));
    return () => {
      live = false;
    };
  }, [days]);

  if (!overview) return null;

  const peak = overview.active_users.reduce((m, p) => Math.max(m, p.users), 0);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Stat icon={<Users className="h-4 w-4" />} label="Peak daily active users" value={String(peak)} note="Exact" />
        <Stat icon={<LogIn className="h-4 w-4" />} label="Sign-ins" value={String(overview.signins)} note="Local sign-ins only" />
        <Stat
          icon={<MousePointerClick className="h-4 w-4" />}
          label="Visitors"
          value={panel?.enabled ? String(panel.visitors ?? 0) : "—"}
          note={panel?.enabled ? "Client-reported" : "OpenPanel not configured"}
        />
      </div>

      <Section title="Active users per day">
        {overview.active_users.length ? (
          <LineChart
            series={[
              {
                name: "Active users",
                color: "var(--accent)",
                points: overview.active_users.map((p) => ({ label: p.date, value: p.users })),
              },
            ]}
          />
        ) : (
          <Empty>No activity recorded yet.</Empty>
        )}
      </Section>

      <Section title="LLM spend by user">
        {overview.by_user.length ? (
          <HBarChart
            rows={overview.by_user.map((u) => ({ label: u.user, value: u.cost_usd }))}
            formatValue={(n) => `$${n.toFixed(2)}`}
          />
        ) : (
          <Empty>No metered usage yet.</Empty>
        )}
      </Section>

      <Section title="Days active by user">
        {overview.user_activity.length ? (
          <HBarChart rows={overview.user_activity.map((u) => ({ label: u.user, value: u.days_active }))} />
        ) : (
          <Empty>No activity recorded yet.</Empty>
        )}
      </Section>
    </div>
  );
}

function Stat({ icon, label, value, note }: { icon: React.ReactNode; label: string; value: string; note: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="flex items-center gap-2 text-muted">
        {icon}
        <span className="text-xs uppercase tracking-wide">{label}</span>
      </div>
      <div className="mt-2 font-display text-2xl">{value}</div>
      <div className="mt-1 text-xs text-muted">{note}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <h3 className="mb-3 text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-muted">{children}</p>;
}
```

Chart props follow `components/charts.tsx` exactly: `LineChart` takes `series: {name, color, points}[]`, `HBarChart` takes `rows: {label, value}[]`.

- [ ] **Step 2: Add the tab**

In `dashboard/src/app/analytics/page.tsx`, extend the `TABS` array (line 40-45):

```tsx
const TABS = [
  { key: "reliability", label: "Reliability · SLO" },
  { key: "cost", label: "Cost" },
  { key: "quality", label: "Usage & Quality" },
  { key: "platform", label: "Platform" },
  { key: "admin", label: "Admin" },
] as const;
```

Add the import:

```tsx
import { AdminPanel } from "@/components/admin-panel";
```

Add state that decides whether the tab button renders — the API is the authority:

```tsx
  // The Admin tab exists only if the API grants it. A non-admin gets 403
  // and never sees the button; there is no client-side role check.
  const [isAdmin, setIsAdmin] = useState(false);
  useEffect(() => {
    api.admin
      .overview(1)
      .then(() => setIsAdmin(true))
      .catch(() => setIsAdmin(false));
  }, []);
```

Filter the rendered tab buttons (in the `TABS.map(...)` at line ~175, wrap the source):

```tsx
        {TABS.filter((t) => t.key !== "admin" || isAdmin).map((t) => (
```

And add the render block after the `platform` block (after line 560's block closes):

```tsx
      {tab === "admin" && isAdmin && <AdminPanel days={days} />}
```

- [ ] **Step 3: Typecheck and build**

Run: `cd dashboard && npx tsc --noEmit && npm run build`
Expected: both succeed.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/admin-panel.tsx dashboard/src/app/analytics/page.tsx
git commit -m "feat(dashboard): add admin tab to analytics"
```

---

### Task 10: Trial meter, onboarding demos, and upgrade prompt

**Files:**
- Create: `dashboard/src/components/trial-banner.tsx`
- Create: `dashboard/src/lib/demo-ideas.ts`
- Modify: `dashboard/src/components/mission-control-shell.tsx` (render the banner inside the shell)
- Test: `dashboard/src/lib/demo-ideas.test.ts`

**Interfaces:**
- Consumes: `api.getTrialStatus()` (pre-existing, api.ts:535), `trialTone`, `TrialStatus` (Task 8).
- Produces: `<TrialBanner />`; `DEMO_IDEAS: DemoIdea[]`; `shouldShowOnboarding(seenKey, status)`.

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/lib/demo-ideas.test.ts`:

```ts
import test from "node:test";
import assert from "node:assert/strict";

import { DEMO_IDEAS, shouldShowOnboarding } from "./demo-ideas";

const fresh = { trial_enabled: true, budget: 100, used: 0, remaining: 100, exhausted: false, warning: false };
const spent = { trial_enabled: true, budget: 100, used: 100, remaining: 0, exhausted: true, warning: true };

test("every demo idea has a title, blurb and href", () => {
  assert.ok(DEMO_IDEAS.length >= 3);
  for (const idea of DEMO_IDEAS) {
    assert.ok(idea.title.length > 0);
    assert.ok(idea.blurb.length > 0);
    assert.ok(idea.href.startsWith("/"));
  }
});

test("onboarding shows for a fresh trial that has not been seen", () => {
  assert.equal(shouldShowOnboarding(false, fresh), true);
});

test("onboarding does not show once dismissed", () => {
  assert.equal(shouldShowOnboarding(true, fresh), false);
});

test("onboarding does not show when the budget is already spent", () => {
  // Suggestions are only actionable while tokens remain.
  assert.equal(shouldShowOnboarding(false, spent), false);
});

test("onboarding does not show when trials are disabled", () => {
  assert.equal(
    shouldShowOnboarding(false, { trial_enabled: false, budget: 0, used: 0, remaining: 0, exhausted: false, warning: false }),
    false,
  );
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd dashboard && npx tsx --test src/lib/demo-ideas.test.ts`
Expected: FAIL — cannot find module `./demo-ideas`

- [ ] **Step 3: Implement the ideas module**

Create `dashboard/src/lib/demo-ideas.ts`:

```ts
/**
 * Things worth trying on the free allowance.
 *
 * These are shown at onboarding, while the user still has tokens to spend
 * on them — attaching suggestions to exhaustion would surface them at the
 * exact moment they stop being actionable.
 */

import type { TrialStatus } from "./admin-api";

export interface DemoIdea {
  title: string;
  blurb: string;
  href: string;
}

export const DEMO_IDEAS: DemoIdea[] = [
  {
    title: "Run a pipeline on a repo",
    blurb: "Point DevAI at a repository and watch the ALM stages work through it end to end.",
    href: "/runs",
  },
  {
    title: "Compose a crew",
    blurb: "Assemble agents into a crew and give it a task to work through.",
    href: "/compose",
  },
  {
    title: "Try an agent in a sandbox",
    blurb: "Author an agent and evaluate it in an isolated sandbox before promoting it.",
    href: "/sandboxes",
  },
  {
    title: "Compose a workflow",
    blurb: "Chain agents into a workflow and run it against a sample task.",
    href: "/workflows",
  },
];

/** Show the onboarding panel only while the suggestions can still be acted on. */
export function shouldShowOnboarding(alreadySeen: boolean, status: TrialStatus | null | undefined): boolean {
  if (alreadySeen) return false;
  if (!status || !status.trial_enabled) return false;
  return !status.exhausted;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd dashboard && npx tsx --test src/lib/demo-ideas.test.ts`
Expected: PASS (5 tests)

- [ ] **Step 5: Implement the banner**

Create `dashboard/src/components/trial-banner.tsx`:

```tsx
"use client";

/**
 * Free-allowance surface: an onboarding panel with things to try, a
 * persistent remaining-tokens meter, an >=80% warning, and an exhaustion
 * prompt pointing at Settings.
 *
 * All state comes from GET /api/settings/trial, which already computes the
 * warning threshold and exhaustion — this component maps, it does not
 * re-derive. The call is `soft`, so a 401 never bounces a signed-in user
 * off the page they're on.
 */

import { useEffect, useState } from "react";
import Link from "next/link";

import { api, type TrialStatus } from "@/lib/api";
import { trialTone } from "@/lib/admin-api";
import { DEMO_IDEAS, shouldShowOnboarding } from "@/lib/demo-ideas";

const SEEN_KEY = "devai-trial-onboarding-seen";

export function TrialBanner() {
  const [status, setStatus] = useState<TrialStatus | null>(null);
  const [seen, setSeen] = useState(true);

  useEffect(() => {
    setSeen(window.localStorage.getItem(SEEN_KEY) === "1");
    api
      .getTrialStatus()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  const tone = trialTone(status);
  if (tone === "hidden" || !status) return null;

  const dismiss = () => {
    window.localStorage.setItem(SEEN_KEY, "1");
    setSeen(true);
  };

  if (shouldShowOnboarding(seen, status)) {
    return (
      <div className="mb-4 rounded-lg border border-border bg-surface p-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-display text-base">You have {status.remaining.toLocaleString()} free tokens</h3>
            <p className="mt-1 text-sm text-muted">
              They run on the platform&apos;s own model providers. Here are a few things worth trying.
            </p>
          </div>
          <button onClick={dismiss} className="text-xs text-muted underline">
            Dismiss
          </button>
        </div>
        <ul className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {DEMO_IDEAS.map((idea) => (
            <li key={idea.href} className="rounded border border-border p-3">
              <Link href={idea.href} className="text-sm font-semibold hover:underline">
                {idea.title}
              </Link>
              <p className="mt-1 text-xs text-muted">{idea.blurb}</p>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (tone === "exhausted") {
    return (
      <div className="mb-4 rounded-lg border border-border bg-surface p-4">
        <h3 className="font-display text-base">Your free allowance is used up</h3>
        <p className="mt-1 text-sm text-muted">
          Add your own model provider key to keep going.{" "}
          <Link href="/settings" className="underline">
            Open Settings
          </Link>
        </p>
      </div>
    );
  }

  const pct = status.budget > 0 ? Math.min(100, Math.round((status.used / status.budget) * 100)) : 0;
  return (
    <div className="mb-4 flex items-center gap-3 rounded-lg border border-border bg-surface px-4 py-2 text-xs">
      <span className="text-muted">Free allowance</span>
      <span className="h-1.5 w-32 overflow-hidden rounded bg-border">
        <span className="block h-full bg-accent" style={{ width: `${pct}%` }} />
      </span>
      <span>{status.remaining.toLocaleString()} left</span>
      {tone === "warning" && (
        <Link href="/settings" className="ml-auto underline">
          Add your own key
        </Link>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Render it in the shell**

In `dashboard/src/components/mission-control-shell.tsx`, render `<TrialBanner />` immediately above the `{children}` slot in the main content area (not on `/login`, which the shell already passes through). Add the import:

```tsx
import { TrialBanner } from "@/components/trial-banner";
```

- [ ] **Step 7: Typecheck, build, and commit**

```bash
cd dashboard && npx tsc --noEmit && npm run build
git add dashboard/src/components/trial-banner.tsx dashboard/src/lib/demo-ideas.ts dashboard/src/lib/demo-ideas.test.ts dashboard/src/components/mission-control-shell.tsx
git commit -m "feat(dashboard): surface free allowance and demo ideas"
```

---

### Task 11: Configuration defaults and documentation

**Files:**
- Modify: `helm/devai/values.yaml` (env block)
- Modify: `docs/PLATFORM-ARCHITECTURE.md` (one subsection)

**Interfaces:** none — configuration only.

- [ ] **Step 1: Set the non-enforcing defaults**

In `helm/devai/values.yaml`, add to the existing env/config block:

```yaml
  # Platform owners — grants the "admin" role, which gates /api/admin/*.
  DEVAI_ADMIN_EMAILS: "samyak.rout@gmail.com,mahesh.sangawar@gmail.com"

  # Free allowance shown to users. The meter is VISIBLE but NOT ENFORCED:
  # enforcement needs DEVAI_LLM_REQUIRE_USER_CONNECTOR=true, deliberately
  # left false until real consumption is read off the admin tab. Trial
  # counters never reset, so an exhausted user is permanently revoked from
  # the shared keys — the budget is chosen from evidence, not guessed.
  DEVAI_LLM_TRIAL_TOKEN_BUDGET: "200000"
  DEVAI_LLM_REQUIRE_USER_CONNECTOR: "false"
```

- [ ] **Step 2: Document the surfaces**

In `docs/PLATFORM-ARCHITECTURE.md`, add a subsection:

```markdown
### Admin analytics and the trial allowance

`/api/admin/*` is gated by a router-level admin-role dependency
(`src/devai/admin/routes.py`), granted through `DEVAI_ADMIN_EMAILS`. It
reports daily active users (exact, from `audit_log`), sign-ins, per-user
LLM spend (from the Redis usage ledger), and — when configured — page-level
stats proxied from OpenPanel.

Production never observes a login: auth-bff terminates OAuth outside the
pod, so the backend counts *active users*, not sign-ins. The dashboard
labels the two separately rather than conflating them.

The trial allowance (`src/devai/settings/trial.py`) is visible but
unenforced. `DEVAI_LLM_TRIAL_TOKEN_BUDGET` sets what the meter shows;
enforcement additionally requires `DEVAI_LLM_REQUIRE_USER_CONNECTOR=true`.
Trial counters are permanent by design — exhaustion revokes the shared keys
for that user for good.

Onboarding OpenPanel requires a `devai` project in
`tesserix-k8s/charts/thirdparty/openpanel/values-prod.yaml` and its client
ID in the `openpanel-client-ids` secret; until then the section reports
`enabled: false`.
```

- [ ] **Step 3: Run the full verification**

```bash
python -m pytest tests/unit -q
ruff check src/
cd dashboard && npx tsc --noEmit && npm run build
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add helm/devai/values.yaml docs/PLATFORM-ARCHITECTURE.md
git commit -m "chore(admin): configure admin emails and trial budget"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Backend as system of record, OpenPanel as enrichment | 2, 3, 4 |
| Admin tab on `/analytics`, enforced server-side | 1, 5, 9 |
| `require_admin` at router level | 1 |
| `GET /api/admin/overview` | 5 |
| `GET /api/admin/openpanel`, disabled-by-default | 4, 5 |
| `ActivityMiddleware`, one row/user/day, best-effort | 2, 6 |
| Reuse `audit_log`, no new SQL | 2, 3 |
| Active users vs sign-ins labelled distinctly | 3, 7, 9 |
| Trial provider, onboarding demos, meter, ≥80% warning, exhaustion prompt | 10 |
| No trial backend changes | — (none planned) |
| Config: admin emails, non-zero budget, strict mode off | 11 |
| Tests: 403 per route, DAU dedup, degradation, banner thresholds | 1, 2, 3, 4, 8, 10 |
| Out of scope: `tesserix-k8s`, strict mode, deploys | Global Constraints |

No gaps.

**Placeholder scan:** No TBD/TODO. Every code step carries real code. One deliberate exception is called out inline: Task 9 Step 1 contains a stray character in a section title, flagged in the step itself with the correct text to use.

**Type consistency:** `TrialStatus` is defined once in `admin-api.ts` and re-exported from `api.ts`, so both `trial-banner.tsx` and `demo-ideas.ts` see one type. `record_active`/`record_login` return `bool` in both the tests and implementation. `active_users_timeseries`/`signin_count`/`active_user_totals` keep identical names across Task 3 (definition), Task 5 (import and monkeypatch targets), and the tests. `AdminOverview` field names match the Task 5 payload keys exactly (`days`, `active_users`, `signins`, `user_activity`, `by_user`, `enabled`).
