"""Noop GitOps backend — tests, disabled mode, and the graceful-degrade fallback."""

from __future__ import annotations

from typing import Any

from devai.adapters.gitops.base import GitOpsAdapter, err

_DETAIL = "gitops adapter is not configured (provider=noop)"


class NoopGitOpsAdapter(GitOpsAdapter):
    provider = "noop"

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider, "detail": _DETAIL}

    async def list_targets(self, scope: str = "") -> list[dict[str, Any]]:
        return []

    async def get_target(self, name: str, scope: str = "") -> dict[str, Any]:
        return err(_DETAIL)

    async def sync(self, name: str, scope: str = "") -> dict[str, Any]:
        return err(_DETAIL)

    async def history(self, name: str, scope: str = "") -> list[dict[str, Any]]:
        return []

    async def rollback(self, name: str, revision: str = "", scope: str = "") -> dict[str, Any]:
        return err(_DETAIL)


__all__ = ["NoopGitOpsAdapter"]
