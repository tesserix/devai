"""Hondo memory adapter.

Wraps the Hondo agentic-memory service. Like mem0 and zep, the SDK is
lazy-imported so installs that don't use Hondo never pay the dependency
cost.

Hondo's API maps cleanly to our ABC:

    self._client.add(text=..., user_id=..., metadata=...)        → remember
    self._client.search(query=..., user_id=..., limit=...)       → semantic_search
    self._client.list(user_id=..., limit=...)                    → recall (no query)
    self._client.delete(memory_id=...)                           → forget

We pack (agent, repo) into Hondo's `user_id` partition the same way the
mem0/zep adapters do, so per-agent + per-repo isolation works without
needing a schema change on Hondo's side.

SDK package: `hondo` (or `hondo-sdk` depending on release line). The
import block tries both names so users don't have to know which one
their dep resolver pulled.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

logger = logging.getLogger(__name__)


def _user_id(agent: str | None, repo: str | None) -> str:
    a = agent or "global"
    r = repo or "global"
    return f"{a}::{r}"


class HondoMemoryAdapter(MemoryAdapter):
    """Adapter over the Hondo client SDK."""

    provider_name = "hondo"

    def __init__(self, *, url: str = "", api_key: str = "") -> None:
        if not api_key and not url:
            raise AdapterNotConfigured("hondo adapter requires DEVAI_HONDO_API_KEY or DEVAI_HONDO_URL")

        # The SDK exposes the client under one of several module names
        # depending on the release line. Try the common ones in order.
        client = None
        last_err: Exception | None = None
        for mod_name, ctor_name in (
            ("hondo", "Hondo"),
            ("hondo", "HondoClient"),
            ("hondo_sdk", "HondoClient"),
            ("hondo.client", "HondoClient"),
        ):
            try:
                module = __import__(mod_name, fromlist=[ctor_name])
                ctor = getattr(module, ctor_name)
                client = ctor(api_key=api_key or None, base_url=url or None)
                break
            except (ImportError, AttributeError) as e:
                last_err = e
                continue

        if client is None:
            raise AdapterNotInstalled(
                "hondo adapter requires `pip install hondo` (or `hondo-sdk`) — falling back to Noop"
            ) from last_err

        self._client = client
        self._url = url
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
                text=content,
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
            raw = await _maybe_await(self._client.search(query=query, **kwargs))
        else:
            # Hondo's listing API name varies across releases — try the common ones
            raw = None
            for method_name in ("list", "list_memories", "get_all"):
                method = getattr(self._client, method_name, None)
                if method is not None:
                    raw = await _maybe_await(method(**kwargs))
                    break
            if raw is None:
                return []
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
        if not provider_id:
            return False
        try:
            await _maybe_await(self._client.delete(memory_id=provider_id))
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── Adapter contract ──────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        try:
            # No dedicated ping; a tiny list with limit=1 against a probe
            # user is the cheapest available call across SDK versions.
            for method_name in ("list", "list_memories", "get_all"):
                method = getattr(self._client, method_name, None)
                if method is not None:
                    await _maybe_await(method(user_id="health::probe", limit=1))
                    break
            return {
                "ok": True,
                "provider": self.provider_name,
                "detail": f"url={self._url or 'cloud'}",
            }
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
        # Hondo returns either a list or a {results: [...]} envelope across versions.
        if isinstance(raw, dict):
            raw = raw.get("results") or raw.get("memories") or []
        if not isinstance(raw, list):
            return []

        wanted_type = MemoryType.parse(memory_type) if memory_type else None
        out: list[MemoryRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            mt = MemoryType.parse(meta.get("memory_type"))
            if wanted_type and mt != wanted_type:
                continue
            out.append(
                MemoryRecord(
                    content=item.get("text") or item.get("content") or item.get("memory") or "",
                    agent=agent or "",
                    repo=repo or "global",
                    memory_type=mt,
                    tags=list(meta.get("tags") or []),
                    metadata={k: v for k, v in meta.items() if k not in {"memory_type", "tags", "relevance_score"}},
                    relevance_score=float(meta.get("relevance_score", 1.0) or 1.0),
                    similarity=item.get("score") or item.get("similarity"),
                    provider_id=str(item.get("id") or item.get("memory_id") or ""),
                    created_at=_parse_created(item.get("created_at")) or time.time(),
                    provider=self.provider_name,
                )
            )
            if len(out) >= limit:
                break
        return out


def _extract_id(result: Any) -> str | None:
    """Hondo returns varying shapes per release — be defensive."""
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
    """Hondo's client can be sync or async depending on the version — handle both."""
    import inspect as _inspect

    if _inspect.isawaitable(value):
        return await value
    return value


__all__ = ["HondoMemoryAdapter"]
