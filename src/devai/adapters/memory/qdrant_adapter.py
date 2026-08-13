"""Qdrant memory adapter — the cluster's dedicated vector store.

Talks to Qdrant's REST API over httpx (already a core dependency) rather
than `qdrant-client`, so the backend needs no extra SDK in the image and
no lazy-import dance.

Mapping to the ABC:

    PUT  /collections/{c}/points          → remember (upsert one point)
    POST /collections/{c}/points/scroll   → recall (payload filters)
    POST /collections/{c}/points/search   → semantic_search (vector + filters)
    POST /collections/{c}/points/delete   → forget
    POST /collections/{c}/points/payload  → reinforce (read-modify-write)

Everything but the vector lives in the point payload — agent, repo,
memory_type, tags, metadata, relevance_score, access_count, created_at —
so recall's filters are pushed into Qdrant instead of being applied here.
The one exception is `query`, matched as a substring on the way out:
Qdrant's text match needs a full-text payload index, and building one per
collection is a schema commitment this adapter doesn't need to make.

An embedder is mandatory (the factory refuses to build without one): a
store that holds only vectors has nothing to offer if every vector is a
placeholder.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "devai_memories"


class QdrantMemoryAdapter(MemoryAdapter):
    """Adapter over a Qdrant collection of memory points."""

    provider_name = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        embedder: Any,
        collection: str = DEFAULT_COLLECTION,
        api_key: str = "",
        dimensions: int = 1536,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._embedder = embedder
        self._collection = collection
        self._dimensions = int(getattr(embedder, "dimensions", 0) or dimensions)
        self._client = client
        self._own_client = client is None
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._collection_ready = False

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
        record = MemoryRecord(
            content=content,
            agent=agent,
            repo=repo,
            memory_type=mt,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
            created_at=time.time(),
            provider_id=str(uuid.uuid4()),
            provider=self.provider_name,
        )

        await self._ensure_collection()
        vector = await self._embedder.embed(content)
        await self._request(
            "PUT",
            f"/collections/{self._collection}/points",
            json={"points": [{"id": record.provider_id, "vector": list(vector), "payload": _to_payload(record)}]},
            params={"wait": "true"},
        )
        return record

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
        body: dict[str, Any] = {"limit": limit, "with_payload": True}
        filt = _build_filter(agent=agent, repo=repo, memory_type=memory_type, tags=tags)
        if filt:
            body["filter"] = filt
        # Substring matching happens here, so ask for enough rows that a
        # narrow query still has candidates to match against.
        if query:
            body["limit"] = max(limit * 10, 50)

        data = await self._read(f"/collections/{self._collection}/points/scroll", body)
        if data is None:
            return []
        points = data.get("result", {}).get("points", [])
        records = [_to_record(p) for p in points]
        if query:
            needle = query.lower()
            records = [r for r in records if needle in r.content.lower()]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        try:
            vector = await self._embedder.embed(query)
        except Exception:  # noqa: BLE001
            logger.exception("qdrant: embedding the query failed — no semantic hits")
            return []

        body: dict[str, Any] = {"vector": list(vector), "limit": k, "with_payload": True}
        filt = _build_filter(agent=agent, repo=repo, memory_type=memory_type, tags=None)
        if filt:
            body["filter"] = filt

        data = await self._read(f"/collections/{self._collection}/points/search", body)
        if data is None:
            return []
        return [_to_record(hit) for hit in data.get("result", [])]

    # ── Update / delete ───────────────────────────────────────────────

    async def reinforce(self, provider_ids: list[str]) -> int:
        """Qdrant has no server-side arithmetic, so bump payloads in place.

        Capped at 2.0 for the same reason as pgvector: a frequently-recalled
        memory must not drown out everything else in the ranking.
        """
        if not provider_ids:
            return 0
        points = await self._retrieve(provider_ids)
        updated = 0
        for point in points:
            payload = point.get("payload") or {}
            new_payload = {
                "relevance_score": min(float(payload.get("relevance_score", 1.0) or 1.0) + 0.1, 2.0),
                "access_count": int(payload.get("access_count", 0) or 0) + 1,
                "last_accessed": time.time(),
            }
            try:
                await self._request(
                    "POST",
                    f"/collections/{self._collection}/points/payload",
                    json={"payload": new_payload, "points": [point["id"]]},
                    params={"wait": "true"},
                )
                updated += 1
            except Exception:  # noqa: BLE001
                logger.exception("qdrant: reinforcing %s failed", point.get("id"))
        return updated

    async def forget(self, provider_id: str) -> bool:
        if not provider_id:
            return False
        if not await self._retrieve([provider_id]):
            return False
        try:
            await self._request(
                "POST",
                f"/collections/{self._collection}/points/delete",
                json={"points": [provider_id]},
                params={"wait": "true"},
            )
        except Exception:  # noqa: BLE001
            logger.exception("qdrant: deleting %s failed", provider_id)
            return False
        return True

    # ── Adapter contract ──────────────────────────────────────────────

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("qdrant client close failed", exc_info=True)
            self._client = None
        closer = getattr(self._embedder, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.debug("embedder close failed", exc_info=True)

    async def health_check(self) -> dict[str, Any]:
        try:
            response = await self._request("GET", f"/collections/{self._collection}", allow_404=True)
            if response.status_code == 404:
                return {
                    "ok": True,
                    "provider": self.provider_name,
                    "detail": f"{self._url}: collection {self._collection} not created yet",
                }
            count = (response.json().get("result") or {}).get("points_count")
            return {"ok": True, "provider": self.provider_name, "detail": f"{self._url}: {count} points"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}

    # ── Internal ──────────────────────────────────────────────────────

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            import httpx

            headers = {"api-key": self._api_key} if self._api_key else {}
            self._client = httpx.AsyncClient(base_url=self._url, headers=headers, timeout=self._timeout)
            self._own_client = True
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        response = await self._http().request(method, path, json=json, params=params)
        if allow_404 and response.status_code == 404:
            return response
        response.raise_for_status()
        return response

    async def _read(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """POST a read query, returning None when the store can't answer."""
        try:
            response = await self._request("POST", path, json=body)
            return dict(response.json())
        except Exception:  # noqa: BLE001
            logger.exception("qdrant: read on %s failed — recalling nothing", path)
            return None

    async def _retrieve(self, provider_ids: list[str]) -> list[dict[str, Any]]:
        try:
            response = await self._request(
                "POST",
                f"/collections/{self._collection}/points",
                json={"ids": [str(p) for p in provider_ids], "with_payload": True},
            )
            return list(response.json().get("result") or [])
        except Exception:  # noqa: BLE001
            logger.exception("qdrant: retrieving %s failed", provider_ids)
            return []

    async def _ensure_collection(self) -> None:
        """Create the collection on first write, at the embedder's width."""
        if self._collection_ready:
            return
        response = await self._request("GET", f"/collections/{self._collection}", allow_404=True)
        if response.status_code == 404:
            await self._request(
                "PUT",
                f"/collections/{self._collection}",
                json={"vectors": {"size": self._dimensions, "distance": "Cosine"}},
            )
            logger.info("qdrant: created collection %s (dim=%s)", self._collection, self._dimensions)
        self._collection_ready = True


def _to_payload(record: MemoryRecord) -> dict[str, Any]:
    return {
        "content": record.content,
        "agent": record.agent,
        "repo": record.repo,
        "memory_type": record.memory_type.value,
        "tags": list(record.tags),
        "metadata": dict(record.metadata),
        "relevance_score": record.relevance_score,
        "access_count": record.access_count,
        "created_at": record.created_at,
    }


def _to_record(point: dict[str, Any]) -> MemoryRecord:
    payload = point.get("payload") or {}
    return MemoryRecord(
        content=payload.get("content", ""),
        agent=payload.get("agent", ""),
        repo=payload.get("repo", "global"),
        memory_type=MemoryType.parse(payload.get("memory_type")),
        tags=list(payload.get("tags") or []),
        metadata=dict(payload.get("metadata") or {}),
        relevance_score=float(payload.get("relevance_score", 1.0) or 1.0),
        similarity=point.get("score"),
        access_count=int(payload.get("access_count", 0) or 0),
        created_at=float(payload.get("created_at") or time.time()),
        provider_id=str(point.get("id", "")),
        provider=QdrantMemoryAdapter.provider_name,
    )


def _build_filter(
    *,
    agent: str | None,
    repo: str | None,
    memory_type: MemoryType | str | None,
    tags: list[str] | None,
) -> dict[str, Any] | None:
    must: list[dict[str, Any]] = []
    if agent:
        must.append({"key": "agent", "match": {"value": agent}})
    if repo:
        # A repo-scoped recall must still see the memories learned globally.
        must.append(
            {
                "should": [
                    {"key": "repo", "match": {"value": repo}},
                    {"key": "repo", "match": {"value": "global"}},
                ]
            }
        )
    if memory_type:
        must.append({"key": "memory_type", "match": {"value": MemoryType.parse(memory_type).value}})
    if tags:
        must.append({"key": "tags", "match": {"any": list(tags)}})
    return {"must": must} if must else None


__all__ = ["QdrantMemoryAdapter"]
