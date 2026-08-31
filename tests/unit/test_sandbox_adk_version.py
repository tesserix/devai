"""A sandbox pins the kit release it runs on, like everything else that moves a result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from devai.kit.versions import AdkVersionCatalogue
from devai.sandbox.models import SandboxSpec
from devai.sandbox.service import SandboxError, SandboxService

_MIN_SPEC: dict[str, Any] = {
    "agent": {"name": "code-remediator-agent", "version": "v1.8.2"},
    "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_sandbox(self, **kw: Any) -> dict[str, Any]:
        self.rows[kw["sandbox_id"]] = {**kw, "id": kw["sandbox_id"]}
        return self.rows[kw["sandbox_id"]]

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        return self.rows.get(sandbox_id)

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.rows[sandbox_id]["status"] = status

    async def touch_sandbox(self, sandbox_id: str, ttl_seconds: int) -> None:
        del ttl_seconds
        self.rows[sandbox_id]["last_access_at"] = datetime.now(UTC)


def _catalogue() -> AdkVersionCatalogue:
    async def fetch() -> list[dict]:
        return [
            {"tag_name": "v0.1.1", "draft": False, "prerelease": False},
            {"tag_name": "v0.1.0", "draft": False, "prerelease": False},
        ]

    return AdkVersionCatalogue(fetch=fetch, fallback="0.1.1")


async def test_an_unpinned_sandbox_records_the_default_kit_version() -> None:
    db = _FakeDB()
    svc = SandboxService(db, adk_catalogue=_catalogue())

    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert rec.spec.adk_version == "0.1.1"
    assert db.rows[rec.id]["spec"]["adk_version"] == "0.1.1"


async def test_an_offered_version_is_honoured() -> None:
    svc = SandboxService(_FakeDB(), adk_catalogue=_catalogue())

    rec = await svc.create(SandboxSpec.model_validate({**_MIN_SPEC, "adk_version": "v0.1.0"}), owner="sam@example.com")

    assert rec.spec.adk_version == "0.1.0"


async def test_a_version_that_is_not_offered_is_refused_at_creation() -> None:
    svc = SandboxService(_FakeDB(), adk_catalogue=_catalogue())

    with pytest.raises(SandboxError, match="0.0.1"):
        await svc.create(SandboxSpec.model_validate({**_MIN_SPEC, "adk_version": "0.0.1"}), owner="sam@example.com")


async def test_without_a_catalogue_the_spec_is_left_as_written() -> None:
    svc = SandboxService(_FakeDB())

    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert rec.spec.adk_version is None
