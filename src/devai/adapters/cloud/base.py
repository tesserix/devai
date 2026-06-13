"""CloudAdapter ABC — GCP / AWS / Azure behind one surface.

Consumes the per-user Cloud Account connectors (Settings → Cloud Account):
agents and tools resolve the caller's account, then read identity and a small
inventory (projects/accounts/subscriptions + regions) without importing a
vendor SDK into business logic. Every method degrades to the family error
shape — never an exception into the agent loop.

SDKs are lazy-imported inside each backend (adapter-family rule); a backend
you don't use never loads its SDK.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


def err(detail: str) -> dict[str, Any]:
    return {"ok": False, "error": detail}


class CloudAdapter(ABC):
    """Minimum surface every cloud backend implements."""

    provider: str = "cloud"

    async def close(self) -> None:
        return

    async def health_check(self) -> dict[str, Any]:
        try:
            ident = await self.identity()
            ok = isinstance(ident, dict) and ident.get("ok", True) is not False
            return {"ok": bool(ok), "provider": self.provider, "detail": str(ident)[:200]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider, "detail": str(e)[:200]}

    @abstractmethod
    async def identity(self) -> dict[str, Any]:
        """Who these credentials are (account/project/subscription + principal)."""

    @abstractmethod
    async def list_scopes(self) -> list[dict[str, Any]]:
        """The billing/ownership scopes: GCP projects / AWS account / Azure subscriptions."""


__all__ = ["CloudAdapter", "err"]
