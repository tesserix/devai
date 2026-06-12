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

_MODELS = "devai:usage:models"
_USERS = "devai:usage:users"
_DAYS = "devai:usage:days"
_MODEL = "devai:usage:model:{key}"
_USER = "devai:usage:user:{key}"
_DAY = "devai:usage:day:{key}"
_TOTAL = "devai:usage:total"
_RECENT = "devai:usage:recent"
_RECENT_MAX = 300


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

            def _bump(pipe: Any, key: str) -> None:
                pipe.hincrby(key, "calls", 1)
                pipe.hincrby(key, "tokens_in", int(tokens_in or 0))
                pipe.hincrby(key, "tokens_out", int(tokens_out or 0))
                pipe.hincrby(key, "cost_micro", cost_u)
                pipe.hincrby(key, "duration_ms", int(duration_ms or 0))
                if status != "ok":
                    pipe.hincrby(key, "errors", 1)

            pipe = client.pipeline(transaction=False)
            _bump(pipe, _TOTAL)
            _bump(pipe, _MODEL.format(key=model_id))
            _bump(pipe, _DAY.format(key=day))
            pipe.hset(_MODEL.format(key=model_id), "provider", provider or "")
            pipe.sadd(_MODELS, model_id)
            pipe.zadd(_DAYS, {day: 0})
            if triggered_by and "@" in triggered_by:
                _bump(pipe, _USER.format(key=triggered_by))
                pipe.sadd(_USERS, triggered_by)
            pipe.lpush(
                _RECENT,
                json.dumps(
                    {
                        "ts": day,
                        "provider": provider,
                        "model": model_id,
                        "agent": agent,
                        "run_id": run_id,
                        "user": triggered_by,
                        "tokens_in": int(tokens_in or 0),
                        "tokens_out": int(tokens_out or 0),
                        "cost_usd": round(cost_usd, 6),
                        "duration_ms": round(duration_ms, 1),
                        "status": status,
                    }
                ),
            )
            pipe.ltrim(_RECENT, 0, _RECENT_MAX - 1)
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

    async def summary(self) -> dict[str, Any]:
        client = await self._client()
        if client is None:
            return self._row({})
        try:
            return self._row(await client.hgetall(_TOTAL))
        except Exception:  # noqa: BLE001
            return self._row({})

    async def by_model(self) -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            ids = sorted(await client.smembers(_MODELS))
            out = []
            for mid in ids:
                h = await client.hgetall(_MODEL.format(key=mid))
                row = self._row(h)
                row["model"] = mid
                row["provider"] = h.get("provider", "")
                out.append(row)
            return sorted(out, key=lambda r: r["cost_usd"], reverse=True)
        except Exception:  # noqa: BLE001
            return []

    async def by_user(self) -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            users = sorted(await client.smembers(_USERS))
            out = []
            for u in users:
                row = self._row(await client.hgetall(_USER.format(key=u)))
                row["user"] = u
                out.append(row)
            return sorted(out, key=lambda r: r["cost_usd"], reverse=True)
        except Exception:  # noqa: BLE001
            return []

    async def timeseries(self, days: int = 30) -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            ds = sorted(await client.zrange(_DAYS, -days, -1))
            out = []
            for d in ds:
                row = self._row(await client.hgetall(_DAY.format(key=d)))
                row["day"] = d
                out.append(row)
            return out
        except Exception:  # noqa: BLE001
            return []

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        client = await self._client()
        if client is None:
            return []
        try:
            raw = await client.lrange(_RECENT, 0, max(0, limit - 1))
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
