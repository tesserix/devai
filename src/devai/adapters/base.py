"""Generic adapter protocol — shared shape for every integration family.

Each family (memory, vector store, event bus, …) declares its own richer
ABC, but they all inherit the two methods on this Protocol so a process-wide
shutdown hook can iterate `(adapter.close() for adapter in adapters)` and
a single liveness probe can iterate `health_check()`.

`AdapterRegistry` is a thin name → factory map. Concrete families use it
to register their `<provider>` names without hardcoding an if/elif tower
in the factory function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)


class AdapterError(Exception):
    """Base class for any adapter-layer failure."""


class AdapterNotConfigured(AdapterError):
    """Raised when an adapter is selected but its required settings are missing.

    Example: `DEVAI_MEMORY_PROVIDER=mem0` but `DEVAI_MEM0_API_KEY` is unset.
    The factory catches this and falls back to Noop with a clear warning,
    so a misconfigured pod never crashes — it degrades.
    """


class AdapterNotInstalled(AdapterError):
    """Raised when an adapter is selected but its SDK isn't on the path.

    Example: `DEVAI_MEMORY_PROVIDER=zep` without `zep-python` installed.
    The factory catches this and falls back to Noop.
    """


@runtime_checkable
class Adapter(Protocol):
    """Bare minimum every adapter family inherits."""

    async def close(self) -> None:
        """Release any owned resources (connection pools, HTTP clients, …).

        Must be idempotent — call twice and the second call is a no-op.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return a small dict describing adapter health.

        Convention: `{"ok": bool, "provider": str, "detail": str}`. Used
        by the FastAPI readiness probe and the dashboard's integrations
        panel. Implementations should not raise — convert failures into
        `ok=False` + a description in `detail`.
        """
        ...


T = TypeVar("T", bound=Adapter)


class AdapterRegistry(Generic[T]):
    """Name → factory map used by each family's `create_*_adapter`.

    Why a class instead of a dict? Two reasons:
      1. The factory functions need a uniform shape (`settings → Adapter`)
         and a registry enforces that.
      2. Tests can swap in fake factories without monkey-patching the
         family's factory module.
    """

    def __init__(self, family: str) -> None:
        self.family = family
        self._factories: dict[str, Callable[[Any], T]] = {}

    def register(self, name: str, factory: Callable[[Any], T]) -> None:
        if name in self._factories:
            raise AdapterError(f"{self.family}: provider {name!r} already registered")
        self._factories[name] = factory

    def register_or_replace(self, name: str, factory: Callable[[Any], T]) -> None:
        """For tests / overrides."""
        self._factories[name] = factory

    def resolve(self, name: str, settings: Any) -> T:
        if name not in self._factories:
            known = ", ".join(sorted(self._factories.keys()))
            raise AdapterError(f"{self.family}: unknown provider {name!r}. Known: {known}")
        return self._factories[name](settings)

    def known(self) -> list[str]:
        return sorted(self._factories.keys())

    def has(self, name: str) -> bool:
        return name in self._factories


__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterNotConfigured",
    "AdapterNotInstalled",
    "AdapterRegistry",
]
