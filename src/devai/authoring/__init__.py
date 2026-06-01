"""User-authoring of agents (specializations) and blueprints.

Lets the dashboard create custom agents — pick tools from the catalog,
set the prompt/model — and workflows, persisting them durably and making
new agents runnable immediately via the YAML specialization runner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devai.authoring.service import AuthoringError, AuthoringService
from devai.authoring.store import (
    DefinitionStore,
    InMemoryDefinitionStore,
    RedisDefinitionStore,
)

if TYPE_CHECKING:
    from devai.specializations.registry import SpecializationRegistry


def create_authoring_service(
    *,
    redis: Any | None = None,
    spec_registry: SpecializationRegistry | None = None,
) -> AuthoringService:
    """Build the service with a Redis-backed store, falling back to in-memory.

    Never raises — an unreachable Redis degrades to the in-memory store so
    the authoring API still works (definitions just don't survive a restart).
    """
    store: DefinitionStore = (
        RedisDefinitionStore(redis) if redis is not None else InMemoryDefinitionStore()
    )
    return AuthoringService(store, spec_registry=spec_registry)


__all__ = [
    "AuthoringService",
    "AuthoringError",
    "DefinitionStore",
    "InMemoryDefinitionStore",
    "RedisDefinitionStore",
    "create_authoring_service",
]
