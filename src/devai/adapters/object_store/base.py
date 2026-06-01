"""ObjectStoreAdapter ABC — blob storage for composer attachments/images.

The Cursor composer lets a user attach images (mockups, screenshots) as
reference. Those blobs land here (GCS in prod, in-memory Noop for tests),
keyed, and the key travels in `task.agent_context["attachments"]`. The
AgentRunner fetches the bytes and passes them to the LLM as multimodal
content. Backends: gcs, s3, local, noop.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from devai.adapters.base import Adapter


@dataclass(slots=True)
class StoredObject:
    key: str
    uri: str = ""  # provider URI (gs://… / s3://…) — empty for noop
    content_type: str = "application/octet-stream"
    size: int = 0


class ObjectStoreAdapter(Adapter):
    """Minimum surface every blob backend implements."""

    provider_name: str = "abstract"

    @abstractmethod
    async def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        """Store bytes under a key. Overwrites if it exists."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Fetch the bytes for a key. Raises KeyError if absent."""

    async def exists(self, key: str) -> bool:
        try:
            await self.get(key)
            return True
        except KeyError:
            return False

    async def signed_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """A time-limited URL the browser can GET. Default: not supported."""
        raise NotImplementedError(f"{self.provider_name} adapter does not implement signed_url")

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "no health check implemented"}


__all__ = ["ObjectStoreAdapter", "StoredObject"]
