"""Google Cloud Storage object-store backend.

Lazy-imports `google-cloud-storage`. Uses Application Default Credentials
(the pod's Workload Identity SA), so no key material lives in config — the
bucket name is the only setting. Blob keys are stored as-is under the
bucket.
"""

from __future__ import annotations

import asyncio
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.object_store.base import ObjectStoreAdapter, StoredObject


class GCSObjectStoreAdapter(ObjectStoreAdapter):
    provider_name = "gcs"

    def __init__(self, *, bucket: str = "", prefix: str = "") -> None:
        if not bucket:
            raise AdapterNotConfigured("gcs object_store requires DEVAI_OBJECT_STORE_BUCKET")
        try:
            from google.cloud import storage  # noqa: F401
        except ImportError as e:
            raise AdapterNotInstalled("gcs object_store requires `pip install google-cloud-storage`") from e
        from google.cloud import storage

        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)

    def _blob_name(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> StoredObject:
        def _do() -> StoredObject:
            blob = self._bucket.blob(self._blob_name(key))
            blob.upload_from_string(body, content_type=content_type)
            return StoredObject(
                key=key,
                uri=f"gs://{self._bucket_name}/{self._blob_name(key)}",
                content_type=content_type,
                size=len(body),
            )

        return await asyncio.to_thread(_do)

    async def get(self, key: str) -> bytes:
        def _do() -> bytes:
            blob = self._bucket.blob(self._blob_name(key))
            if not blob.exists():
                raise KeyError(key)
            return blob.download_as_bytes()

        return await asyncio.to_thread(_do)

    async def signed_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        from datetime import timedelta

        def _do() -> str:
            blob = self._bucket.blob(self._blob_name(key))
            return blob.generate_signed_url(expiration=timedelta(seconds=expires_seconds))

        return await asyncio.to_thread(_do)

    async def health_check(self) -> dict[str, Any]:
        try:
            ok = await asyncio.to_thread(self._bucket.exists)
            return {"ok": bool(ok), "provider": self.provider_name, "detail": f"bucket={self._bucket_name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}


__all__ = ["GCSObjectStoreAdapter"]
