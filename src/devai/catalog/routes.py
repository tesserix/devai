"""The model picker's source of truth — mounted at ``/api/models``."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from devai.catalog.models import CATALOG, default_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


def _connected(request: Request) -> set[str]:
    """Providers with usable credentials right now; empty when it can't be told."""
    settings = getattr(request.app.state, "config", None)
    if settings is None:
        return set()
    try:
        from devai.adapters.llm.capabilities import connected_providers

        return {p.lower() for p in connected_providers(settings)}
    except Exception:  # a picker must still render when probing fails
        logger.debug("could not determine connected providers", exc_info=True)
        return set()


@router.get("")
async def list_models(request: Request) -> dict[str, object]:
    live = _connected(request)
    return {
        "default_provider": default_provider(),
        "providers": [{**p.to_dict(), "connected": pid in live} for pid, p in CATALOG.items()],
    }
