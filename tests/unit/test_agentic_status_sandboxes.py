"""Sandbox storage as a Service-health component.

Sandboxes are a table, not a service, so the only check that means anything is
whether that table is queryable — a missing `sandboxes` relation 500s the page
with nothing on the health board to explain it.
"""

from __future__ import annotations

from typing import Any

import pytest

from devai.agentic.status import probe_sandbox_storage


class _OkService:
    async def health(self) -> dict[str, Any]:
        return {"total": 3, "live": 1}


class _BrokenService:
    async def health(self) -> dict[str, Any]:
        raise RuntimeError('relation "sandboxes" does not exist')


@pytest.mark.asyncio
async def test_reachable_when_storage_answers() -> None:
    cs = await probe_sandbox_storage(_OkService())

    assert cs.reachable is True
    assert cs.role == "storage"
    assert cs.error == ""
    assert cs.detail["total"] == 3
    assert cs.detail["live"] == 1


@pytest.mark.asyncio
async def test_unreachable_and_names_the_cause_when_table_is_missing() -> None:
    cs = await probe_sandbox_storage(_BrokenService())

    assert cs.reachable is False
    assert "sandboxes" in cs.error


@pytest.mark.asyncio
async def test_unconfigured_service_is_reported_not_raised() -> None:
    cs = await probe_sandbox_storage(None)

    assert cs.reachable is False
    assert cs.error == "sandbox service not configured"


@pytest.mark.asyncio
async def test_service_health_counts_by_status() -> None:
    from devai.sandbox.service import SandboxService

    class _DB:
        async def sandbox_counts(self) -> dict[str, int]:
            return {"ready": 2, "destroyed": 4}

    health = await SandboxService(_DB()).health()

    assert health["total"] == 6
    assert health["live"] == 2
    assert health["by_status"] == {"ready": 2, "destroyed": 4}
