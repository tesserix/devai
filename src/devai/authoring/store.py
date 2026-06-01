"""Persistence for user-authored agents (specializations) and blueprints.

Authored definitions are stored as their validated YAML text so the
existing loaders remain the single source of truth for schema. Two
backends, following the project's store convention:

  * :class:`InMemoryDefinitionStore` — tests + graceful fallback.
  * :class:`RedisDefinitionStore` — durable across restarts, reusing the
    Redis that's already wired (no new SQL schema, per CLAUDE.md).

Keyed by ``(kind, name)`` where kind is ``specialization`` | ``blueprint``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class AuthoredDefinition:
    kind: str  # "specialization" | "blueprint"
    name: str
    yaml: str
    created_by: str = "operator"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "yaml": self.yaml,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuthoredDefinition:
        return cls(
            kind=d.get("kind", ""),
            name=d.get("name", ""),
            yaml=d.get("yaml", ""),
            created_by=d.get("created_by", "operator"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


class DefinitionStore(ABC):
    """Minimum surface every backend implements."""

    @abstractmethod
    async def upsert(self, defn: AuthoredDefinition) -> AuthoredDefinition: ...

    @abstractmethod
    async def get(self, kind: str, name: str) -> AuthoredDefinition | None: ...

    @abstractmethod
    async def list(self, kind: str) -> list[AuthoredDefinition]: ...

    @abstractmethod
    async def delete(self, kind: str, name: str) -> bool: ...


class InMemoryDefinitionStore(DefinitionStore):
    """Process-local store. Used in tests and when Redis is unreachable."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], AuthoredDefinition] = {}

    async def upsert(self, defn: AuthoredDefinition) -> AuthoredDefinition:
        existing = self._data.get((defn.kind, defn.name))
        defn.created_at = existing.created_at if existing else (defn.created_at or _utcnow_iso())
        defn.updated_at = _utcnow_iso()
        self._data[(defn.kind, defn.name)] = defn
        return defn

    async def get(self, kind: str, name: str) -> AuthoredDefinition | None:
        return self._data.get((kind, name))

    async def list(self, kind: str) -> list[AuthoredDefinition]:
        return [d for (k, _), d in self._data.items() if k == kind]

    async def delete(self, kind: str, name: str) -> bool:
        return self._data.pop((kind, name), None) is not None


class RedisDefinitionStore(DefinitionStore):
    """Durable store backed by the shared Redis.

    Records live at ``devai:authoring:{kind}:{name}`` (JSON) with a per-kind
    index set ``devai:authoring:index:{kind}`` so list() is a single SMEMBERS.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @staticmethod
    def _key(kind: str, name: str) -> str:
        return f"devai:authoring:{kind}:{name}"

    @staticmethod
    def _index(kind: str) -> str:
        return f"devai:authoring:index:{kind}"

    async def upsert(self, defn: AuthoredDefinition) -> AuthoredDefinition:
        existing = await self.get(defn.kind, defn.name)
        defn.created_at = existing.created_at if existing else (defn.created_at or _utcnow_iso())
        defn.updated_at = _utcnow_iso()
        await self._redis.set(self._key(defn.kind, defn.name), json.dumps(defn.to_dict()))
        await self._redis.sadd(self._index(defn.kind), defn.name)
        return defn

    async def get(self, kind: str, name: str) -> AuthoredDefinition | None:
        raw = await self._redis.get(self._key(kind, name))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return AuthoredDefinition.from_dict(json.loads(raw))

    async def list(self, kind: str) -> list[AuthoredDefinition]:
        names = await self._redis.smembers(self._index(kind))
        out: list[AuthoredDefinition] = []
        for n in names:
            name = n.decode() if isinstance(n, bytes) else n
            defn = await self.get(kind, name)
            if defn is not None:
                out.append(defn)
        return out

    async def delete(self, kind: str, name: str) -> bool:
        removed = await self._redis.delete(self._key(kind, name))
        await self._redis.srem(self._index(kind), name)
        return bool(removed)


__all__ = [
    "AuthoredDefinition",
    "DefinitionStore",
    "InMemoryDefinitionStore",
    "RedisDefinitionStore",
]
