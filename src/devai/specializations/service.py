"""SpecializationService — FastAPI lifecycle wrapper for the registry.

Mirrors PipelineService. Holds one SpecializationRegistry, loaded from
`settings.specializations_dir`. Stored on `app.state.specialization_service`.

Other parts of DevAI (PipelineService, dashboard, chat agent) read from
this service rather than building their own registry — that way a SIGHUP
reload propagates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devai.specializations.registry import (
    SpecializationRegistry,
    SpecializationRegistryError,
)

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.specializations.base import Specialization

logger = logging.getLogger(__name__)


class SpecializationService:
    def __init__(self, config: "Settings", *, directory: str | Path | None = None) -> None:
        self.config = config
        self.directory = Path(directory or getattr(config, "specializations_dir", "specializations"))
        self._registry: SpecializationRegistry | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        try:
            self._registry = SpecializationRegistry.from_directory(self.directory)
            logger.info(
                "SpecializationService loaded %d specializations from %s",
                len(self._registry),
                self.directory,
            )
        except Exception:
            logger.exception("SpecializationService load failed — starting with empty registry")
            self._registry = SpecializationRegistry()
        self._started = True

    async def stop(self) -> None:
        self._started = False
        self._registry = None

    async def reload(self) -> int:
        """Re-discover from disk. Returns the new count.

        Used by the dashboard's "reload specs" action (and a future
        SIGHUP handler) so YAML edits take effect without a restart.
        """
        new_registry = SpecializationRegistry.from_directory(self.directory)
        self._registry = new_registry
        return len(new_registry)

    # ── Read surface ─────────────────────────────────────────────────

    @property
    def registry(self) -> SpecializationRegistry:
        if self._registry is None:
            return SpecializationRegistry()
        return self._registry

    def list_all(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.registry.all()]

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for spec in self.registry.all():
            out.setdefault(spec.category, []).append(spec.to_dict())
        return out

    def get(self, name: str) -> dict[str, Any] | None:
        try:
            return self.registry.resolve(name).to_dict()
        except SpecializationRegistryError:
            return None

    def get_full(self, name: str) -> "Specialization | None":
        try:
            return self.registry.resolve(name)
        except SpecializationRegistryError:
            return None


__all__ = ["SpecializationService"]
