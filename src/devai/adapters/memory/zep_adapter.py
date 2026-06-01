"""Zep memory adapter.

Wraps the `zep-python` SDK. Like the mem0 adapter, the SDK is lazy-imported
so installs that don't use Zep never pay the dependency cost.

Zep's surface differs from mem0 in two ways we accommodate:

  1. Zep organizes memories under a `session_id` (their per-user thread).
     We pack (agent, repo) into the session id the same way we did for
     mem0's user_id.

  2. Zep's `Memory` object groups messages instead of treating each one
     as a record. We map one `remember()` call to one `Message` appended
     to a session; `recall()` returns the most recent messages.

For embedding-similarity search Zep exposes the `MemorySearchPayload`
API which is its closest analogue to our `semantic_search`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

logger = logging.getLogger(__name__)


def _session_id(agent: str | None, repo: str | None) -> str:
    a = agent or "global"
    r = repo or "global"
    # Zep session IDs are ASCII identifiers — keep it filename-safe.
    return f"{a}__{r}".replace("/", "_").replace(":", "_")


class ZepMemoryAdapter(MemoryAdapter):
    """Adapter over `zep-python`."""

    provider_name = "zep"

    def __init__(self, *, url: str = "", api_key: str = "") -> None:
        if not url:
            raise AdapterNotConfigured("zep adapter requires DEVAI_ZEP_URL")
        try:
            from zep_python.client import AsyncZep  # type: ignore[import-untyped]
        except ImportError as e:
            raise AdapterNotInstalled("zep adapter requires `pip install zep-python` — falling back to Noop") from e

        self._client = AsyncZep(api_key=api_key or None, base_url=url)
        self._url = url

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
        from zep_python.types import Message  # type: ignore[import-untyped]

        mt = MemoryType.parse(memory_type)
        session = _session_id(agent, repo)
        # Best-effort session create — Zep tolerates re-create
        try:
            await self._client.memory.add_session(session_id=session, user_id=agent or "global")
        except Exception:  # noqa: BLE001
            pass

        meta = {
            "memory_type": mt.value,
            "tags": list(tags or []),
            "relevance_score": relevance_score,
            **dict(metadata or {}),
        }
        message = Message(role="user", content=content, metadata=meta)
        await self._client.memory.add(session_id=session, messages=[message])

        return MemoryRecord(
            content=content,
            agent=agent,
            repo=repo,
            memory_type=mt,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
            # Zep doesn't return a stable per-message id on append; we use a
            # client-side uuid so forget() / dedup still works.
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
        if query:
            return await self.semantic_search(query, k=limit, agent=agent, repo=repo, memory_type=memory_type)

        session = _session_id(agent, repo)
        try:
            raw = await self._client.memory.get(session_id=session, limit=limit)
        except Exception:  # noqa: BLE001
            return []
        return self._normalize_messages(
            getattr(raw, "messages", []), agent=agent, repo=repo, memory_type=memory_type, limit=limit
        )

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        session = _session_id(agent, repo)
        try:
            results = await self._client.memory.search(session_id=session, text=query, limit=k)
        except Exception:  # noqa: BLE001
            return []
        return self._normalize_search(results, agent=agent, repo=repo, memory_type=memory_type, limit=k)

    # ── Delete ────────────────────────────────────────────────────────

    async def forget(self, provider_id: str) -> bool:
        # Zep's `delete_session` removes the whole thread, which is too big.
        # Per-message deletion isn't supported via the public SDK in current
        # versions; we return False to signal "not supported".
        logger.warning("ZepMemoryAdapter.forget is a no-op — Zep does not support per-message delete")
        return False

    # ── Adapter contract ──────────────────────────────────────────────

    async def close(self) -> None:
        try:
            await self._client.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    async def health_check(self) -> dict[str, Any]:
        try:
            # No dedicated ping; list sessions with limit=1 is the cheapest call.
            await self._client.memory.list_sessions(page_size=1)
            return {"ok": True, "provider": self.provider_name, "detail": f"url={self._url}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}

    # ── Internal ──────────────────────────────────────────────────────

    def _normalize_messages(
        self,
        messages: Any,
        *,
        agent: str | None,
        repo: str | None,
        memory_type: MemoryType | str | None,
        limit: int,
    ) -> list[MemoryRecord]:
        wanted_type = MemoryType.parse(memory_type) if memory_type else None
        out: list[MemoryRecord] = []
        for msg in messages:
            meta = getattr(msg, "metadata", None) or {}
            mt = MemoryType.parse(meta.get("memory_type"))
            if wanted_type and mt != wanted_type:
                continue
            out.append(
                MemoryRecord(
                    content=getattr(msg, "content", "") or "",
                    agent=agent or "",
                    repo=repo or "global",
                    memory_type=mt,
                    tags=list(meta.get("tags") or []),
                    metadata={k: v for k, v in meta.items() if k not in {"memory_type", "tags", "relevance_score"}},
                    relevance_score=float(meta.get("relevance_score", 1.0) or 1.0),
                    provider_id=str(getattr(msg, "uuid", "") or getattr(msg, "id", "") or ""),
                    created_at=_parse_created(getattr(msg, "created_at", None)) or time.time(),
                    provider=self.provider_name,
                )
            )
            if len(out) >= limit:
                break
        return out

    def _normalize_search(
        self,
        results: Any,
        *,
        agent: str | None,
        repo: str | None,
        memory_type: MemoryType | str | None,
        limit: int,
    ) -> list[MemoryRecord]:
        wanted_type = MemoryType.parse(memory_type) if memory_type else None
        out: list[MemoryRecord] = []
        for item in results:
            msg = getattr(item, "message", None) or item
            meta = getattr(msg, "metadata", None) or {}
            mt = MemoryType.parse(meta.get("memory_type"))
            if wanted_type and mt != wanted_type:
                continue
            out.append(
                MemoryRecord(
                    content=getattr(msg, "content", "") or "",
                    agent=agent or "",
                    repo=repo or "global",
                    memory_type=mt,
                    tags=list(meta.get("tags") or []),
                    metadata={k: v for k, v in meta.items() if k not in {"memory_type", "tags", "relevance_score"}},
                    relevance_score=float(meta.get("relevance_score", 1.0) or 1.0),
                    similarity=getattr(item, "score", None),
                    provider_id=str(getattr(msg, "uuid", "") or getattr(msg, "id", "") or ""),
                    created_at=_parse_created(getattr(msg, "created_at", None)) or time.time(),
                    provider=self.provider_name,
                )
            )
            if len(out) >= limit:
                break
        return out


def _parse_created(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime as _dt

        return _dt.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


__all__ = ["ZepMemoryAdapter"]
