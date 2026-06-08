"""mem0 memory adapter.

Wraps the `mem0ai` Python SDK. Supports the cloud-hosted service (set
`DEVAI_MEM0_API_KEY`) and self-hosted mem0 instances (`DEVAI_MEM0_HOST`).

The SDK is imported lazily — `pip install mem0ai` is only required when
the operator selects this provider. If the SDK is missing the factory
catches the ImportError and falls back to Noop.

mem0's native operations map cleanly onto our ABC:

    self._client.add(messages=..., user_id=..., metadata=...)     → remember
    self._client.search(query=..., user_id=..., limit=...)        → semantic_search
    self._client.get_all(user_id=..., filters=...)                → recall
    self._client.delete(memory_id=...)                            → forget

We project mem0's `user_id` from the (agent, repo) pair so per-agent /
per-repo isolation works without a schema change in mem0.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

logger = logging.getLogger(__name__)


def _user_id(agent: str | None, repo: str | None) -> str:
    """Stable key — mem0 uses `user_id` as the partition; we pack (agent,repo) into it."""
    a = agent or "global"
    r = repo or "global"
    return f"{a}::{r}"


class Mem0MemoryAdapter(MemoryAdapter):
    """Adapter over the mem0ai client."""

    provider_name = "mem0"

    def __init__(self, *, api_key: str = "", host: str = "") -> None:
        try:
            from mem0 import Memory, MemoryClient  # type: ignore[import-untyped]
        except ImportError as e:
            raise AdapterNotInstalled("mem0 adapter requires `pip install mem0ai` — falling back to Noop") from e

        if api_key:
            self._client = MemoryClient(api_key=api_key, host=host or None)
        elif host:
            # Self-hosted instance without an API key
            self._client = Memory.from_config({"vector_store": {"provider": "pgvector"}})
        else:
            raise AdapterNotConfigured("mem0 adapter requires DEVAI_MEM0_API_KEY or DEVAI_MEM0_HOST")

        self._host = host
        self._has_api_key = bool(api_key)

    # ── Write ─────────────────────────────────────────────────────────

    async def remember(
        self,
        content: str,
        *,
        agent: str = "",
        repo: str = "global",
        memory_type: MemoryType | str = MemoryType.EPISODIC,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        relevance_score: float = 1.0,
    ) -> MemoryRecord:
        mt = MemoryType.parse(memory_type)
        meta = {
            "memory_type": mt.value,
            "tags": list(tags or []),
            "relevance_score": relevance_score,
            **dict(metadata or {}),
        }
        result = await _maybe_await(
            self._client.add(
                messages=[{"role": "user", "content": content}],
                user_id=_user_id(agent, repo),
                metadata=meta,
            )
        )
        provider_id = _extract_id(result) or ""
        return MemoryRecord(
            content=content,
            agent=agent,
            repo=repo,
            memory_type=mt,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
            provider_id=provider_id,
            provider=self.provider_name,
        )

    # ── Read ──────────────────────────────────────────────────────────

    async def recall(
        self,
        *,
        query: str | None = None,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        kwargs: dict[str, Any] = {"user_id": _user_id(agent, repo), "limit": limit}
        if query:
            # Use mem0's search when a query is supplied (semantic).
            raw = await _maybe_await(self._client.search(query=query, **kwargs))
        else:
            raw = await _maybe_await(self._client.get_all(**kwargs))
        return self._normalize(raw, agent=agent, repo=repo, memory_type=memory_type, limit=limit)

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        raw = await _maybe_await(
            self._client.search(
                query=query,
                user_id=_user_id(agent, repo),
                limit=k,
            )
        )
        return self._normalize(raw, agent=agent, repo=repo, memory_type=memory_type, limit=k)

    # ── Delete ────────────────────────────────────────────────────────

    async def forget(self, provider_id: str) -> bool:
        try:
            await _maybe_await(self._client.delete(memory_id=provider_id))
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── Adapter contract ──────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        try:
            # mem0 doesn't expose a dedicated ping; a tiny get_all is cheap
            await _maybe_await(self._client.get_all(user_id="health::probe", limit=1))
            return {"ok": True, "provider": self.provider_name, "detail": f"host={self._host or 'cloud'}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}

    # ── Internal ──────────────────────────────────────────────────────

    def _normalize(
        self,
        raw: Any,
        *,
        agent: str | None,
        repo: str | None,
        memory_type: MemoryType | str | None,
        limit: int,
    ) -> list[MemoryRecord]:
        if not isinstance(raw, list):
            raw = raw.get("results", []) if isinstance(raw, dict) else []

        wanted_type = MemoryType.parse(memory_type) if memory_type else None
        out: list[MemoryRecord] = []
        for item in raw:
            meta = (item.get("metadata") or {}) if isinstance(item, dict) else {}
            mt = MemoryType.parse(meta.get("memory_type"))
            if wanted_type and mt != wanted_type:
                continue
            out.append(
                MemoryRecord(
                    content=item.get("memory") or item.get("content") or "",
                    agent=agent or "",
                    repo=repo or "global",
                    memory_type=mt,
                    tags=list(meta.get("tags") or []),
                    metadata={k: v for k, v in meta.items() if k not in {"memory_type", "tags", "relevance_score"}},
                    relevance_score=float(meta.get("relevance_score", 1.0) or 1.0),
                    similarity=item.get("score"),
                    provider_id=str(item.get("id") or item.get("memory_id") or ""),
                    created_at=_parse_created(item.get("created_at")) or time.time(),
                    provider=self.provider_name,
                )
            )
            if len(out) >= limit:
                break
        return out


def _extract_id(result: Any) -> str | None:
    """mem0 returns varying shapes per release — be defensive."""
    if isinstance(result, dict):
        return result.get("id") or result.get("memory_id")
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("id") or first.get("memory_id")
    return None


def _parse_created(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        from datetime import datetime as _dt

        return _dt.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


async def _maybe_await(value: Any) -> Any:
    """mem0's client is synchronous in v0; awaitable in v1 — handle both."""
    import inspect as _inspect

    if _inspect.isawaitable(value):
        return await value
    return value


__all__ = ["Mem0MemoryAdapter"]
