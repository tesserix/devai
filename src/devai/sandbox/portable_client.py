"""Protocol client for immutable imported agent runtimes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from devai.a2a import check_service_url, resolve_host_block_check
from devai.sandbox.portable import portable_runtime_endpoint

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.sandbox.models import SandboxRecord

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PortableAgentResult:
    final_text: str
    backend: str
    raw: dict[str, Any]


class PortableRuntimeClient:
    def __init__(
        self,
        config: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_provider: Callable[[SandboxRecord], Awaitable[str]] | None = None,
    ) -> None:
        self._namespace = getattr(config, "k8s_runtime_namespace", "devai") or "devai"
        self._allowed_suffixes = list(getattr(config, "a2a_allowed_url_suffixes", None) or [])
        self._transport = transport
        self._token_provider = token_provider

    async def invoke(
        self,
        record: SandboxRecord,
        *,
        message: str,
        triggered_by: str,
    ) -> PortableAgentResult:
        snapshot = record.spec.import_snapshot
        if snapshot is None:
            raise ValueError("sandbox has no imported portable runtime")
        runtime = snapshot.runtime
        endpoint = portable_runtime_endpoint(record, namespace=self._namespace)
        runtime_type = str(runtime.get("type") or "")
        protocol = str(runtime.get("protocol") or "")
        if runtime_type not in {"container", "remote"} or protocol not in {"a2a", "http"}:
            raise ValueError("portable runtime contract is invalid")
        if runtime_type == "remote":
            check_service_url(endpoint, self._allowed_suffixes)
            await asyncio.to_thread(resolve_host_block_check, endpoint, self._allowed_suffixes)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-DevAI-Sandbox-ID": record.id,
        }
        auth = runtime.get("auth")
        if auth is None:
            auth = {}
        if not isinstance(auth, dict):
            raise ValueError("portable runtime auth contract is invalid")
        if runtime_type == "remote":
            if auth.get("type") != "bearer" or not auth.get("credentialRef"):
                raise ValueError("portable remote agent requires a bearer credential reference")
            if self._token_provider is None:
                raise ValueError("portable remote agent requires an authenticated connector")
            token = await self._token_provider(record)
            if not token:
                raise ValueError("portable remote agent connector returned no credential")
            headers["Authorization"] = f"Bearer {token}"
        elif auth:
            raise ValueError("portable container runtime cannot declare authentication")
        payload = (
            _a2a_payload(message, triggered_by=triggered_by, sandbox_id=record.id)
            if protocol == "a2a"
            else {
                "message": message,
                "context": {"sandbox_id": record.id, "triggered_by": triggered_by},
            }
        )
        timeout = httpx.Timeout(float(record.spec.limits.max_wall_clock_s), connect=10.0)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ValueError("portable agent response exceeds the 2 MiB limit")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError("portable agent returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("portable agent returned a non-object response")
        if body.get("error"):
            error = body["error"]
            code = error.get("code") if isinstance(error, dict) else "unknown"
            raise ValueError(f"portable agent returned error code {code}")
        result = body.get("result", body) if protocol == "a2a" else body
        final_text = _extract_text(result)
        if not final_text:
            raise ValueError("portable agent response contains no text output")
        return PortableAgentResult(
            final_text=final_text,
            backend=f"{runtime_type}:{protocol}",
            raw=body,
        )


def _a2a_payload(message: str, *, triggered_by: str, sandbox_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
                "messageId": uuid.uuid4().hex,
                "metadata": {"sandboxId": sandbox_id, "triggeredBy": triggered_by},
            }
        },
    }


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    for key in ("final_text", "output", "text"):
        text = value.get(key)
        if isinstance(text, str):
            return text
    for key in ("parts", "artifacts", "message", "status"):
        text = _extract_text(value.get(key))
        if text:
            return text
    return ""


__all__ = ["PortableAgentResult", "PortableRuntimeClient"]
