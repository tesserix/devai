"""FastAPI boundary for optional durable Agent lifecycle execution."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from devai.orchestration.agent_lifecycle_client import AgentLifecycleValidationError

logger = logging.getLogger(__name__)


def require_idempotency_key(request: Request, value: str | None) -> str:
    orchestrator = getattr(request.app.state, "agent_lifecycle_orchestrator", None)
    if orchestrator is None:
        return (value or "").strip()
    key = (value or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required for durable lifecycle mutations")
    if len(key) > 255:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not exceed 255 characters")
    return key


async def durable_result(
    request: Request,
    operation: str,
    call: Callable[[Any], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | None:
    orchestrator = getattr(request.app.state, "agent_lifecycle_orchestrator", None)
    if orchestrator is None:
        return None
    try:
        return await call(orchestrator)
    except AgentLifecycleValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - durable dependency failure is mapped at the HTTP boundary
        config = getattr(request.app.state, "config", None)
        if bool(getattr(config, "temporal_fail_closed", False)):
            raise HTTPException(status_code=503, detail=f"durable {operation} workflow unavailable") from error
        logger.exception("durable %s workflow failed; using the idempotent local path", operation)
        return None


__all__ = ["durable_result", "require_idempotency_key"]
