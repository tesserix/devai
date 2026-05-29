"""Factory helpers for the onboarding store + service.

Never raises: an unreachable Postgres degrades to the in-memory store so
the API renders an empty state instead of crashing the pod. The
reconciler can rebuild a fresh store from the marker files anyway.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.onboarding.service import OnboardingService
from devai.onboarding.store import (
    InMemoryOnboardingStore,
    OnboardingStore,
    PostgresOnboardingStore,
)

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


def create_onboarding_store(pool: Any | None) -> OnboardingStore:
    """Postgres store when a live asyncpg pool is supplied, else in-memory."""
    if pool is not None:
        return PostgresOnboardingStore(pool)
    logger.info("Onboarding: no DB pool — using in-memory store (reconciler rebuilds from markers)")
    return InMemoryOnboardingStore()


def create_onboarding_service(
    settings: Settings,
    scm: Any,
    *,
    pool: Any | None = None,
    store: OnboardingStore | None = None,
) -> OnboardingService:
    org = (
        getattr(settings, "scm_organization", "")
        or getattr(settings, "github_org", "")
        or "tesserix"
    )
    return OnboardingService(
        scm=scm,
        store=store or create_onboarding_store(pool),
        org=org,
    )
