"""Object-store adapter family — blob storage for composer attachments."""

from devai.adapters.object_store.base import ObjectStoreAdapter, StoredObject
from devai.adapters.object_store.factory import (
    create_object_store_adapter,
    object_store_registry,
)
from devai.adapters.object_store.noop import NoopObjectStoreAdapter

__all__ = [
    "NoopObjectStoreAdapter",
    "ObjectStoreAdapter",
    "StoredObject",
    "create_object_store_adapter",
    "object_store_registry",
]
