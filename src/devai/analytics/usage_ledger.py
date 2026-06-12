"""LLM usage ledger — queryable cost / tokens / latency for every call.

Blueprint runs persist to Redis, not ``agent_executions``, so the Postgres
cost views miss them. This ledger fixes that at the one point EVERY call
crosses: the instrumented LLM adapter. Each call atomically increments
per-model, per-user and per-day rollups (Redis hashes via HINCRBY) plus a
capped recent-calls list, so analytics can show real money, tokens and time
broken down by model and by user — for all runs.

Cost is stored as integer micro-USD (USD × 1e6) to keep the counters exact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_USERS = "devai:usage:users"
_RECENT_MAX = 300


def _ns(user: str = "") -> str:
    """Key namespace: global, or per-user when ``user`` is given.

    Per-user keys (``devai:usage:u:<email>:…``) are what make analytics
    tenant-isolated — a non-admin only ever reads their own namespace.
    """
    return f"devai:usage:u:{user}:" if user else "devai:usage:"


def _micro(usd: float) -> int:
    return int(round(max(0.0, usd) * 1_000_000))


def _from_micro(v: Any) -> float:
    return round(int(v or 0) / 1_000_000, 6)


class UsageLedger:
    """Redis-backed usage accumulator. Best-effort: never raises."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._redis: Any = None

    async def _client(self) -> Any:
        if self._redis is None and self._url:
            try:
                import redis.asyncio as redis  # noqa: PLC0415

                self._redis = redis.from_url(self._url, decode_responses=True)
            except Exception:  # noqa: BLE001
                logger.warning("usage ledger: redis unavailable", exc_info=True)
                self._redis = False
        return self._redis or None

    async def record(
        self,
        *,
        day: str,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        duration_ms: float,
        triggered_by: str = "",
        agent: str = "",
        run_id: str = "",
        status: str = "ok",
    ) -> None:
        client = await self._client()
        if client is None:
            return
        try:
            cost_u = _micro(cost_usd)
            model_id = (model or "unknown").strip()
            errors = 1 if status != "ok" else 0

            def _bump(pipe: Any, key: str) -> None:
                pipe.hincrby(key, "calls", 1)
                pipe.hincrby(key, "tokens_in", int(tokens_in or 0))
                pipe.hincrby(key, "tokens_out", int(tokens_out or 0))
                pipe.hincrby(key, "cost_micro", cost_u)
                pipe.hincrby(key, "duration_ms", int(duration_ms or 0))
                if errors:
                    pipe.hincrby(key, "errors", 1)

            def _bump_scope(pipe: Any, prefix: str) -> None:
                """One scope's rollups (global, or a single user's)."""
                _bump(pipe, f"{prefix}total")
                _bump(pipe, f"{prefix}model:{model_id}")
                _bump(pipe, f"{prefix}day:{day}")
                pipe.hset(f"{prefix}model:{model_id}", "provider", provider or "")
                pipe.sadd(f"{prefix}models", model_id)
                pipe.zadd(f"{prefix}days", {day: 0})

            pipe = client.pipeline(transaction=False)
            _bump_scope(pipe, _ns())  # global (admin view)
            user = triggered_by if (triggered_by and "@" in triggered_by) else ""
            if user:
                _bump_scope(pipe, _ns(user))  # tenant-isolated view
                pipe.sadd(_USERS, user)
            rec = json.dumps(
                {
                    "ts": day, "provider": provider, "model": model_id, "agent": agent,
                    "run_id": run_id, "user": user,
                    "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                    "cost_usd": round(cost_usd, 6), "duration_ms": round(duration_ms, 1),
                    "status": status,
                }
            )
            pipe.lpush(f"{_ns()}recent", rec)
            pipe.ltrim(f"{_ns()}recent", 0, _RECENT_MAX - 1)
            if user:
                pipe.lpush(f"{_ns(user)}recent", rec)
                pipe.ltrim(f"{_ns(user)}recent", 0, _RECENT_MAX - 1)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("usage ledger: record failed", exc_info=True)

    # ── reads (analytics) ─────────────────────────────────────────────

    @staticmethod
    def _row(h: dict[str, Any]) -> dict[str, Any]:
        return {
            "calls": int(h.get("calls", 0) or 0),
            "tokens_in": int(h.get("tokens_in", 0) or 0),
            "tokens_out": int(h.get("tokens_out", 0) or 0),
            "cost_usd": _from_micro(h.get("cost_micro", 0)),
            "duration_ms": int(h.get("duration_ms", 0) or 0),
            "errors": int(h.get("errors", 0) or 0),
        }

    async def summary(self, user: str = "") -> dict[str, Any]:
        client = await self._client()
        if client is None:
            return self._row({})
        try:
            return self._row(await client.hgetall(f"{_ns(user)}total"))
        except Exception:  # noqa: BLE001
            return self._row({})

    async def by_model(self, user: str = "") -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            ids = sorted(await client.smembers(f"{_ns(user)}models"))
            out = []
            for mid in ids:
                h = await client.hgetall(f"{_ns(user)}model:{mid}")
                row = self._row(h)
                row["model"] = mid
                row["provider"] = h.get("provider", "")
                out.append(row)
            return sorted(out, key=lambda r: r["cost_usd"], reverse=True)
        except Exception:  # noqa: BLE001
            return []

    async def by_user(self) -> list[dict[str, Any]]:
        """All users' totals — ADMIN ONLY (global cross-tenant view)."""
        client = await self._client()
        if client is None:
            return []
        try:
            users = sorted(await client.smembers(_USERS))
            out = []
            for u in users:
                row = self._row(await client.hgetall(f"{_ns(u)}total"))
                row["user"] = u
                out.append(row)
            return sorted(out, key=lambda r: r["cost_usd"], reverse=True)
        except Exception:  # noqa: BLE001
            return []

    async def timeseries(self, days: int = 30, user: str = "") -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            ds = sorted(await client.zrange(f"{_ns(user)}days", -days, -1))
            out = []
            for d in ds:
                row = self._row(await client.hgetall(f"{_ns(user)}day:{d}"))
                row["day"] = d
                out.append(row)
            return out
        except Exception:  # noqa: BLE001
            return []

    async def recent(self, limit: int = 100, user: str = "") -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            raw = await client.lrange(f"{_ns(user)}recent", 0, max(0, limit - 1))
            return [json.loads(r) for r in raw]
        except Exception:  # noqa: BLE001
            return []


_global: UsageLedger | None = None


def set_global_ledger(ledger: UsageLedger | None) -> None:
    global _global  # noqa: PLW0603
    _global = ledger


def get_global_ledger() -> UsageLedger | None:
    return _global


__all__ = ["UsageLedger", "get_global_ledger", "set_global_ledger"]
