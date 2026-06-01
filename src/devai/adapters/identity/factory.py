"""Identity adapter factory — selects the backend from settings.

``DEVAI_AUTH_PROVIDER`` drives the choice:

  local_db            -> LocalDBIdentityAdapter (kind sandbox only)
  keycloak | github | gip | <anything else>
                      -> NoopIdentityAdapter (auth terminated upstream)

Never raises: an unknown/misconfigured provider degrades to Noop so the pod
boots and prod auth (auth-bff) is unaffected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from devai.adapters.identity.noop import NoopIdentityAdapter

if TYPE_CHECKING:
    from devai.adapters.identity.base import IdentityAdapter
    from devai.config import Settings

logger = logging.getLogger(__name__)


def create_identity_adapter(settings: Settings, pool: object | None = None) -> IdentityAdapter:
    """Select the identity backend. ``pool`` is the asyncpg pool the local_db
    backend reads its seeded ``devai_local_users`` table from; when absent it
    falls back to the in-memory roster from ``DEVAI_LOCAL_AUTH_USERS``."""
    provider = (getattr(settings, "auth_provider", "") or "").lower()
    if provider == "local_db":
        try:
            from devai.adapters.identity.local_db import LocalDBIdentityAdapter

            users = getattr(settings, "local_auth_users", None) or []
            logger.info("identity: local_db backend (db_pool=%s, env_users=%d)", pool is not None, len(users))
            return LocalDBIdentityAdapter(pool=pool, users=users)
        except Exception:  # noqa: BLE001 — never let auth wiring crash the pod
            logger.exception("identity: local_db init failed — falling back to noop")
            return NoopIdentityAdapter()
    return NoopIdentityAdapter()


__all__ = ["create_identity_adapter"]
