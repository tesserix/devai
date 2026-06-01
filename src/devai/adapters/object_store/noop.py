"""In-memory object store — used for tests and when storage is disabled.

Keeps blobs in a process-local dict. Not durable; fine for unit tests and
single-pod dev. Production uses the GCS backend.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.object_store.base import ObjectStoreAdapter, StoredObject


class NoopObjectStoreAdapter(ObjectStoreAdapter):
    provider_name = "noop"

    def __init__(self) -> None:
        self._blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        self._blobs[key] = (bytes(body), content_type)
        return StoredObject(key=key, content_type=content_type, size=len(body))

    async def get(self, key: str) -> bytes:
        if key not in self._blobs:
            raise KeyError(key)
        return self._blobs[key][0]

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": f"{len(self._blobs)} blobs (ephemeral)"}


__all__ = ["NoopObjectStoreAdapter"]
