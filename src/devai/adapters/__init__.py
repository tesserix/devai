"""Integration adapters — pluggable backends behind stable interfaces.

The adapter pattern keeps DevAI independent of any specific vendor SDK.
For each integration concern (memory, vector store, event bus, secrets,
object store, …) we define one ABC + one factory + N concrete backends.
The rest of the codebase talks only to the ABC; the factory reads a
single env var to choose which backend instance to hand out.

Today's families:

    adapters.memory   — Agentic AI memory (mem0 | zep | pgvector | redis | noop)

Planned (slot in with no churn elsewhere):

    adapters.vector_store     — embeddings (pgvector | pinecone | qdrant | weaviate | chroma)
    adapters.event_bus        — pub/sub (nats | redis_streams | kafka | inproc)
    adapters.object_store     — blobs (s3 | gcs | azure_blob | local)
    adapters.secrets          — runtime secret resolution (gcp_sm | aws_sm | vault | env)
    adapters.telemetry        — traces/metrics (otel | langsmith | datadog | noop)

The convention every family follows:

    adapters/<family>/
      __init__.py       — public re-exports
      base.py           — ABC + canonical record dataclasses
      factory.py        — create_<family>_adapter(settings) reads a provider env var
      <provider>.py     — concrete implementation, lazy-imports its SDK

Lazy SDK imports mean a backend you don't use is never imported, so
adding mem0 to the codebase doesn't force every install to pull it in.
"""

from devai.adapters.base import (
    Adapter,
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterNotConfigured",
    "AdapterNotInstalled",
    "AdapterRegistry",
]
