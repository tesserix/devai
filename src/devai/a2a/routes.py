"""Authenticated A2A server routes for the DevAI specialization catalog."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Annotated, Literal, Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from devai.authz import require_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a/v1", tags=["a2a"])

_AGENT_NAME = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_Text = Annotated[str, Field(min_length=1, max_length=65_536)]
_Parts = Annotated[list["TextPart"], Field(min_length=1, max_length=32)]
_RequestId = (
    Annotated[str, Field(max_length=128)]
    | Annotated[
        int,
        Field(ge=-(2**63), le=2**63 - 1),
    ]
)


class _BoundaryModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TextPart(_BoundaryModel):
    kind: Literal["text"]
    text: _Text


class A2AMessage(_BoundaryModel):
    role: Literal["user"]
    parts: _Parts
    message_id: Annotated[str, Field(alias="messageId", max_length=128)] = ""
    context_id: Annotated[str, Field(alias="contextId", max_length=128)] = ""
    task_id: Annotated[str, Field(alias="taskId", max_length=128)] = ""

    @model_validator(mode="after")
    def message_within_budget(self) -> Self:
        if sum(len(part.text.encode("utf-8")) for part in self.parts) > 262_144:
            raise ValueError("message exceeds 256 KiB")
        return self


class A2AParams(_BoundaryModel):
    message: A2AMessage


class A2ARequest(_BoundaryModel):
    jsonrpc: Literal["2.0"]
    request_id: _RequestId = Field(alias="id")
    method: Literal["message/send"]
    params: A2AParams


def _normalize_agent_name(name: str) -> str | None:
    normalized = name.strip().lower()
    if len(normalized) > 128 or not _AGENT_NAME.fullmatch(normalized):
        return None
    return normalized.removesuffix("-agent").replace("-", "_")


@router.post("/{agent_name}")
async def send_message(agent_name: str, body: A2ARequest, request: Request) -> dict[str, object]:
    principal = await require_principal(request)
    normalized_name = _normalize_agent_name(agent_name)
    service = getattr(request.app.state, "specialization_service", None)
    if normalized_name is None or service is None:
        raise HTTPException(status_code=404, detail="agent not found")
    spec = service.get_full(normalized_name)
    if spec is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if spec.risk_level.needs_human_gate:
        raise HTTPException(status_code=409, detail="agent requires workflow approval")

    deps = getattr(getattr(request.app.state, "pipeline_service", None), "stage_deps", None)
    if deps is None:
        raise HTTPException(status_code=503, detail="agent runtime unavailable")

    run_id = f"a2a-{uuid.uuid4().hex}"
    trace_id = uuid.uuid4().hex
    state = {
        "run_id": run_id,
        "requirements": "\n\n".join(part.text for part in body.params.message.parts),
        "trigger_actor": principal.email,
        "principal": principal.to_dict(),
        "team_id": principal.primary_team_id,
        "trace_id": trace_id,
    }
    try:
        patch = await service.invoke(normalized_name, state, deps=deps)
    except Exception:  # noqa: BLE001 -- HTTP boundary must return a stable, generic error
        logger.exception("A2A agent invocation failed agent=%s trace_id=%s", normalized_name, trace_id)
        raise HTTPException(status_code=500, detail="agent invocation failed") from None
    if patch is None:
        raise HTTPException(status_code=404, detail="agent not found")

    artifact_text = json.dumps(patch, indent=2, sort_keys=True, default=str)
    return {
        "jsonrpc": "2.0",
        "id": body.request_id,
        "result": {
            "id": run_id,
            "contextId": body.params.message.context_id or run_id,
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": uuid.uuid4().hex,
                    "name": f"{normalized_name}-result",
                    "parts": [{"kind": "text", "text": artifact_text}],
                }
            ],
        },
    }


__all__ = ["router"]
