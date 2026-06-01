"""FastAPI routes for the agentic control plane.

Mounted at ``/api/agentic/*``. The dashboard's Gateway panel reads
``GET /api/agentic/status`` and renders a card per component. The
``/proxy/*`` sub-routes forward to the in-cluster service so the
dashboard can fetch component-specific data (e.g. an agent's logs
from kagent) over the user's existing GIP session, without a second
auth round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devai.agentic.status import fetch_agentic_status, to_dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agentic", tags=["agentic"])


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Aggregated snapshot of the four agentic control-plane services.

    Returns a single JSON object the dashboard renders into the
    Gateway panel — no per-component fetches from the browser. The
    probes run in a thread (httpx is sync) so the event loop stays
    responsive when one upstream is slow.
    """
    client = getattr(request.app.state, "registry_client", None)
    snapshot = await asyncio.to_thread(
        fetch_agentic_status,
        registry_client=client,
        agentgateway_url=os.environ.get("DEVAI_AGENTGATEWAY_URL", "")
        or _default("http://agentgateway.agentgateway-system.svc.cluster.local:9092"),
        ai_gateway_url=os.environ.get("DEVAI_AI_GATEWAY_URL", "")
        or _default("http://ai-gateway.agentgateway-system.svc.cluster.local:8080"),
        kagent_url=os.environ.get("DEVAI_KAGENT_CONTROLLER_URL", "")
        or _default("http://kagent-controller.kagent-system.svc.cluster.local:8083"),
    )
    return to_dict(snapshot)


@router.get("/llm-probe")
async def llm_probe(request: Request) -> dict[str, Any]:
    """End-to-end test: dial the LLM gateway from devai-api, fire a
    1-token Anthropic call, return the result. Used as a smoke-test
    button in the Gateway panel."""
    try:
        from devai.config import settings
        from devai.adapters.llm import (
            LLMMessage,
            LLMRequest,
            LLMRole,
            create_llm_adapter,
        )
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"adapters unavailable: {e}") from e

    adapter = create_llm_adapter(settings)
    req = LLMRequest(
        system="Reply in exactly two words.",
        messages=[LLMMessage(role=LLMRole.USER, content="Gateway routing status?")],
        max_tokens=16,
    )
    try:
        resp = await adapter.generate(req)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "adapter": type(adapter).__name__,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
    return {
        "ok": True,
        "adapter": type(adapter).__name__,
        "provider": getattr(adapter, "provider_name", "?"),
        "model": resp.model,
        "text": resp.text,
        "usage": {
            "input": resp.usage.input_tokens,
            "output": resp.usage.output_tokens,
        },
    }


class KagentDispatchBody(BaseModel):
    message: str = Field(..., min_length=1, description="Task/intent to hand the kagent agent")
    namespace: str = Field("", description="Override the kagent namespace (default from settings)")


@router.post("/kagent/{agent}/dispatch")
async def kagent_dispatch(request: Request, agent: str, body: KagentDispatchBody) -> dict[str, Any]:
    """Hand a task to a kagent-managed (long-lived) agent over A2A.

    Additive to the default Job-dispatch path — used for agents the kagent
    agent-sync has reconciled into managed Deployments. 503s cleanly when
    kagent isn't wired (no kagent_url)."""
    from devai.agentic.kagent_client import KagentError, create_kagent_client
    from devai.config import settings
    from devai.identity import extract_principal, trace_id_from_request

    client = create_kagent_client(settings)
    if client is None:
        raise HTTPException(status_code=503, detail="kagent is not configured (no kagent_url)")

    principal = await extract_principal(request)
    triggered_by = ""
    if principal is not None:
        triggered_by = str(getattr(principal, "email", "") or getattr(principal, "uid", "") or "")
    try:
        result = await client.dispatch(
            agent,
            body.message,
            namespace=body.namespace or None,
            triggered_by=triggered_by,
            trace_id=trace_id_from_request(request) or "",
        )
    except KagentError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"agent": agent, "result": result}


def _default(url: str) -> str:
    return url
