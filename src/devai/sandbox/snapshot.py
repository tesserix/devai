"""What a sandbox leaves behind when it is reaped.

A workspace lives minutes; a failing eval case is looked at days later. The
snapshot is the difference between "it failed" and being able to see what the
agent actually did — the diff it produced and the tree it left.

Every probe is independent: a workspace that is already half-gone still yields
whatever it can answer, and never turns teardown into an error.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DIFF_COMMAND = "git diff --no-color"
_STATUS_COMMAND = "git status --porcelain"
_TIMEOUT_S = 30.0


async def capture(client: Any) -> dict[str, Any]:
    """Everything worth keeping from one workspace, best-effort."""
    errors: dict[str, str] = {}

    async def probe(name: str, coro: Any, default: Any) -> Any:
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 — a snapshot is what we could get, not a demand
            errors[name] = str(e)
            return default

    diff = await probe("diff", client.exec(_DIFF_COMMAND, timeout=_TIMEOUT_S), {})
    status = await probe("status", client.exec(_STATUS_COMMAND, timeout=_TIMEOUT_S), {})
    entries = await probe("entries", client.list("."), [])

    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "diff": str(diff.get("stdout", "")),
        "status": str(status.get("stdout", "")),
        "entries": entries,
        "errors": errors,
    }


__all__ = ["capture"]
