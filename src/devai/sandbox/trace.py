"""The trace spine — prompt → LLM → tool → response, with what each step cost.

This is the evidence a sandbox produces. Every metric the platform reports
(correctness, tool selection, groundedness, p95 latency, cost per run) is
derived from these steps rather than measured separately, so the number on a
dashboard can always be traced back to the step that produced it.

Trace metadata lives in Redis with the sandbox's own TTL; payloads above the
inline limit use a durable object-store adapter. Without Redis the store keeps
metadata in process, which is enough for tests and a single-replica dev run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "devai:sandbox"
_GLOBAL_PREFIX = "devai:trace"
_DEFAULT_INLINE_LIMIT_BYTES = 64 * 1024


@dataclass(slots=True)
class TraceStep:
    """One step of an invocation. `kind` is the spine: prompt | llm | tool | response."""

    kind: str
    name: str = ""
    input: Any = None
    output: Any = None
    # Tool steps only — which gateway mode served the call (real/mock/replay/block).
    mode: str = ""
    provider: str = ""
    prompt_version: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Invocation:
    id: str
    sandbox_id: str
    agent: str
    message: str = ""
    final_text: str = ""
    ok: bool = True
    error: str = ""
    execution_backend: str = "inline"
    steps: list[TraceStep] = field(default_factory=list)
    wall_clock_ms: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def totals(self) -> dict[str, Any]:
        return {
            "prompt_tokens": sum(s.prompt_tokens for s in self.steps),
            "completion_tokens": sum(s.completion_tokens for s in self.steps),
            "total_tokens": sum(s.prompt_tokens + s.completion_tokens for s in self.steps),
            "cost_usd": round(sum(s.cost_usd for s in self.steps), 6),
            "latency_ms": sum(s.latency_ms for s in self.steps),
            "wall_clock_ms": self.wall_clock_ms,
            "llm_calls": sum(1 for s in self.steps if s.kind == "llm"),
            "tool_calls": sum(1 for s in self.steps if s.kind == "tool"),
            "blocked_tool_calls": sum(1 for s in self.steps if s.kind == "tool" and s.mode == "block"),
            "steps": len(self.steps),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sandbox_id": self.sandbox_id,
            "agent": self.agent,
            "message": self.message,
            "final_text": self.final_text,
            "ok": self.ok,
            "error": self.error,
            "execution_backend": self.execution_backend,
            "created_at": self.created_at,
            "wall_clock_ms": self.wall_clock_ms,
            "steps": [s.to_dict() for s in self.steps],
            "totals": self.totals,
        }

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> Invocation:
        known = {f for f in TraceStep.__slots__}
        steps = [TraceStep(**{k: v for k, v in s.items() if k in known}) for s in body.get("steps") or []]
        return cls(
            id=str(body.get("id") or ""),
            sandbox_id=str(body.get("sandbox_id") or ""),
            agent=str(body.get("agent") or ""),
            message=str(body.get("message") or ""),
            final_text=str(body.get("final_text") or ""),
            ok=bool(body.get("ok", True)),
            error=str(body.get("error") or ""),
            execution_backend=str(body.get("execution_backend") or "inline"),
            steps=steps,
            wall_clock_ms=int(body.get("wall_clock_ms") or (body.get("totals") or {}).get("wall_clock_ms") or 0),
            created_at=str(body.get("created_at") or ""),
        )


class TraceStore:
    def __init__(
        self,
        redis: Any | None,
        *,
        object_store: Any | None = None,
        inline_limit_bytes: int = _DEFAULT_INLINE_LIMIT_BYTES,
    ) -> None:
        self._redis = redis
        self._object_store = object_store
        self._inline_limit_bytes = max(1, inline_limit_bytes)
        self._local: dict[str, str] = {}
        self._local_index: dict[str, list[str]] = {}

    def _key(self, sandbox_id: str, invocation_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:invocation:{invocation_id}"

    def _index_key(self, sandbox_id: str) -> str:
        return f"{_PREFIX}:{sandbox_id}:invocations"

    def _global_key(self, invocation_id: str) -> str:
        return f"{_GLOBAL_PREFIX}:{invocation_id}"

    async def save(self, invocation: Invocation, *, ttl_seconds: int) -> None:
        key = self._key(invocation.sandbox_id, invocation.id)
        index = self._index_key(invocation.sandbox_id)
        body = json.dumps(invocation.to_dict())
        stored_body = await self._offload(invocation, body)
        if self._redis is None:
            self._local[key] = stored_body
            self._local[self._global_key(invocation.id)] = invocation.sandbox_id
            self._local_index.setdefault(index, []).insert(0, invocation.id)
            return
        try:
            await self._redis.set(key, stored_body, ex=ttl_seconds)
            await self._redis.set(self._global_key(invocation.id), invocation.sandbox_id, ex=ttl_seconds)
            await self._redis.lpush(index, invocation.id)
            await self._redis.expire(index, ttl_seconds)
        except Exception:  # noqa: BLE001 — a lost trace must not fail the invocation
            logger.warning("sandbox trace: save failed for %s", invocation.id, exc_info=True)

    async def get(self, sandbox_id: str, invocation_id: str) -> Invocation | None:
        key = self._key(sandbox_id, invocation_id)
        try:
            body = self._local.get(key) if self._redis is None else await self._redis.get(key)
        except Exception:  # noqa: BLE001
            logger.warning("sandbox trace: read failed for %s", invocation_id, exc_info=True)
            return None
        return await self._decode_stored(body)

    async def get_by_id(self, invocation_id: str) -> Invocation | None:
        key = self._global_key(invocation_id)
        try:
            sandbox_id = self._local.get(key) if self._redis is None else await self._redis.get(key)
        except Exception:  # noqa: BLE001
            logger.warning("sandbox trace: global read failed for %s", invocation_id, exc_info=True)
            return None
        if isinstance(sandbox_id, bytes):
            sandbox_id = sandbox_id.decode()
        if not sandbox_id:
            return None
        return await self.get(str(sandbox_id), invocation_id)

    async def list_for_sandbox(self, sandbox_id: str, *, limit: int = 50) -> list[Invocation]:
        index = self._index_key(sandbox_id)
        try:
            if self._redis is None:
                ids = self._local_index.get(index, [])[:limit]
            else:
                ids = [str(i) for i in await self._redis.lrange(index, 0, limit - 1)]
        except Exception:  # noqa: BLE001
            logger.warning("sandbox trace: index read failed for %s", sandbox_id, exc_info=True)
            return []
        found = [await self.get(sandbox_id, i) for i in ids]
        return [inv for inv in found if inv is not None]

    async def _offload(self, invocation: Invocation, body: str) -> str:
        if (
            self._object_store is None
            or getattr(self._object_store, "provider_name", "noop") == "noop"
            or len(body.encode()) <= self._inline_limit_bytes
        ):
            return body
        object_key = f"sandbox-traces/{invocation.sandbox_id}/{invocation.id}.json"
        try:
            await self._object_store.put(object_key, body.encode(), content_type="application/json")
        except Exception:  # noqa: BLE001 — preserve the trace inline when blob storage degrades
            logger.warning(
                "sandbox trace: object-store write failed for %s; keeping inline", invocation.id, exc_info=True
            )
            return body
        return json.dumps({"object_key": object_key})

    async def _decode_stored(self, body: Any) -> Invocation | None:
        if not body:
            return None
        raw = body.decode() if isinstance(body, bytes) else str(body)
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError:
            return _decode(raw)
        object_key = metadata.get("object_key") if isinstance(metadata, dict) else None
        if not object_key:
            return _decode(raw)
        if self._object_store is None:
            return None
        try:
            return _decode(await self._object_store.get(str(object_key)))
        except Exception:  # noqa: BLE001 — missing/corrupt blob reads as an absent trace
            logger.warning("sandbox trace: object-store read failed for %s", object_key, exc_info=True)
            return None


def _decode(body: Any) -> Invocation | None:
    if not body:
        return None
    if isinstance(body, bytes):
        body = body.decode()
    try:
        return Invocation.from_dict(json.loads(body))
    except Exception:  # noqa: BLE001 — a corrupt record reads as absent
        logger.warning("sandbox trace: corrupt record skipped", exc_info=True)
        return None


__all__ = ["Invocation", "TraceStep", "TraceStore"]
