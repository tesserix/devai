"""Noop cloud backend — tests, disabled mode, graceful-degrade fallback."""

from __future__ import annotations

from typing import Any

from devai.adapters.cloud.base import CloudAdapter, err

_DETAIL = "cloud adapter is not configured (provider=noop)"


class NoopCloudAdapter(CloudAdapter):
    provider = "noop"

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider, "detail": _DETAIL}

    async def identity(self) -> dict[str, Any]:
        return err(_DETAIL)

    async def list_scopes(self) -> list[dict[str, Any]]:
        return []


__all__ = ["NoopCloudAdapter"]
