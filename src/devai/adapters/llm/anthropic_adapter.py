"""Anthropic LLM adapter.

Wraps the `anthropic` Python SDK. Lazy-imported so installs that don't
use Claude never pay the dep cost.

Maps DevAI's normalized `LLMRequest` onto the Messages API:

    request.system           → top-level `system` parameter
    request.messages         → `messages` list, role-translated
    request.tools            → Anthropic tool schema (`input_schema`)
    request.model            → default if empty
    request.max_tokens       → required by Anthropic; default 4096
    request.temperature/top_p → passed through

Response normalization:

    content blocks (text)    → response.text
    content blocks (tool_use) → response.tool_calls
    stop_reason              → response.finish_reason
    usage{input,output,cache_*} → LLMUsage
"""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.llm.base import (
    LLMAdapter,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMUsage,
    ToolCall,
)
from devai.adapters.llm.gateway_routing import gateway_headers

logger = logging.getLogger(__name__)


class AnthropicLLMAdapter(LLMAdapter):
    """Adapter over `anthropic.AsyncAnthropic`."""

    provider_name = "anthropic"
    default_model = "claude-sonnet-5"
    default_max_tokens = 8192

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        default_model: str = "",
        default_max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        gateway_routed: bool = False,
    ) -> None:
        if not api_key:
            raise AdapterNotConfigured("anthropic adapter requires DEVAI_ANTHROPIC_API_KEY")
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-untyped]
        except ImportError as e:
            raise AdapterNotInstalled(
                "anthropic adapter requires `pip install anthropic` — falling back to Noop"
            ) from e

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        self._client = AsyncAnthropic(**kwargs)
        self._gateway_routed = gateway_routed

        if default_model:
            self.default_model = default_model
        if default_max_tokens is not None:
            self.default_max_tokens = default_max_tokens

    async def generate(self, request: LLMRequest) -> LLMResponse:
        kwargs = self._build_kwargs(request)
        started = time.monotonic()
        try:
            raw = await self._client.messages.create(**kwargs)
        except Exception:  # noqa: BLE001 — preserve original error
            logger.exception("anthropic adapter generate() failed")
            raise

        latency_ms = (time.monotonic() - started) * 1000.0
        return self._normalize(raw, latency_ms)

    async def list_models(self) -> list[dict[str, str]]:
        """Anthropic's /v1/models — every model this key can call."""
        try:
            out: list[dict[str, str]] = []
            async for m in self._client.models.list(limit=100):
                mid = getattr(m, "id", "") or ""
                if mid:
                    out.append({"id": mid, "display_name": getattr(m, "display_name", "") or mid})
            return out
        except Exception:  # noqa: BLE001
            logger.warning("anthropic adapter list_models failed", exc_info=True)
            return []

    async def health_check(self) -> dict[str, Any]:
        try:
            # Tiny ping — 1-token completion is the cheapest sanity check
            await self._client.messages.create(
                model=self.default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return {
                "ok": True,
                "provider": self.provider_name,
                "detail": f"model={self.default_model}",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}

    async def close(self) -> None:
        try:
            close_fn = getattr(self._client, "close", None)
            if close_fn is not None:
                result = close_fn()
                if hasattr(result, "__await__"):
                    await result
        except Exception:  # noqa: BLE001
            pass

    # ── Internal ──────────────────────────────────────────────────────

    def _build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        # SYSTEM messages go to the top-level `system` param, not the
        # messages list (Anthropic API). PROMOTE them rather than drop them —
        # silently discarding a caller's system message erased agent personas.
        system_texts = [m.content for m in request.messages if m.role == LLMRole.SYSTEM and m.content]
        wire_messages = [self._message_to_wire(m) for m in request.messages if m.role != LLMRole.SYSTEM]

        # Anthropic requires non-empty messages — if the caller passed
        # only system text, synthesize a placeholder user turn.
        if not wire_messages:
            wire_messages = [{"role": "user", "content": "."}]

        kwargs: dict[str, Any] = {
            "model": request.model or self.default_model,
            "max_tokens": request.max_tokens or self.default_max_tokens,
            "messages": wire_messages,
        }
        combined_system = "\n\n".join(filter(None, [request.system, *system_texts]))
        if combined_system:
            kwargs["system"] = combined_system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop_sequences:
            kwargs["stop_sequences"] = list(request.stop_sequences)
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters or {"type": "object", "properties": {}},
                }
                for t in request.tools
            ]
        # Per-call extras — ONLY parameters the Messages API actually accepts.
        # Callers across the codebase use `extra` for OUR bookkeeping too
        # (`extra={"agent": "plan_approval"}` for telemetry attribution);
        # forwarding those verbatim made the SDK raise `unexpected keyword
        # argument 'agent'` — which silently broke every adapter-based LLM
        # call that passed it (plan-approval clarity → always "clear",
        # recovery diagnosis → "no usable diagnosis", boardroom panel →
        # every seat absent). Bookkeeping keys map to Anthropic metadata.
        _api_keys = {"metadata", "top_k", "stop_sequences", "service_tier", "betas"}
        for k, v in (request.extra or {}).items():
            if k in _api_keys:
                kwargs.setdefault(k, v)
            elif k == "agent" and isinstance(v, str):
                kwargs.setdefault("metadata", {"user_id": f"devai:{v}"[:64]})
        if getattr(self, "_gateway_routed", False):
            headers = gateway_headers(request.extra, provider=self.provider_name)
            if headers:
                kwargs["extra_headers"] = headers
        return kwargs

    def _message_to_wire(self, m: Any) -> dict[str, Any]:
        # role mapping — Anthropic accepts only "user" / "assistant"; TOOL
        # responses are flagged as a tool_result content block under the
        # user role.
        if m.role == LLMRole.TOOL:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content,
                    }
                ],
            }
        # An assistant turn that called tools must be re-emitted as content
        # blocks: an optional text block followed by one tool_use block per
        # call. Without this, the matching tool_result on the next turn has
        # no tool_use to bind to and Anthropic returns a 400.
        tool_calls = getattr(m, "tool_calls", None)
        if m.role == LLMRole.ASSISTANT and tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": dict(tc.arguments or {}),
                    }
                )
            return {"role": "assistant", "content": blocks}
        wire_role = "assistant" if m.role == LLMRole.ASSISTANT else "user"
        # A user turn carrying composer image uploads becomes a content list:
        # the text block followed by one image block per attachment.
        images = getattr(m, "images", None)
        if wire_role == "user" and images:
            content_blocks: list[dict[str, Any]] = []
            if m.content:
                content_blocks.append({"type": "text", "text": m.content})
            for img in images:
                data = img.get("data")
                if not data:
                    continue
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data": data,
                        },
                    }
                )
            if content_blocks:
                return {"role": "user", "content": content_blocks}
        return {"role": wire_role, "content": m.content}

    def _normalize(self, raw: Any, latency_ms: float) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in getattr(raw, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", "") or "",
                        name=getattr(block, "name", "") or "",
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )

        usage_raw = getattr(raw, "usage", None)
        usage = LLMUsage()
        if usage_raw is not None:
            usage.prompt_tokens = int(getattr(usage_raw, "input_tokens", 0) or 0)
            usage.completion_tokens = int(getattr(usage_raw, "output_tokens", 0) or 0)
            usage.cached_tokens = int(
                getattr(usage_raw, "cache_read_input_tokens", 0) or getattr(usage_raw, "cached_tokens", 0) or 0
            )
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(getattr(raw, "stop_reason", "") or ""),
            model=str(getattr(raw, "model", "") or ""),
            provider=self.provider_name,
            request_id=str(getattr(raw, "id", "") or ""),
            latency_ms=latency_ms,
        )


__all__ = ["AnthropicLLMAdapter"]
