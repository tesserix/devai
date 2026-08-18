"""OpenAI LLM adapter.

Wraps the `openai` Python SDK. Lazy-imported so installs that don't
use OpenAI never pay the dep cost.

Maps DevAI's normalized `LLMRequest` onto the Chat Completions API:

    request.system            → first message with role="system"
    request.messages          → `messages` list, role passed through
    request.tools             → `tools[]` in OpenAI's function-call format
    request.model             → default if empty
    request.max_tokens        → optional cap
    request.temperature/top_p → passed through
    request.response_format   → e.g. {"type": "json_object"}

Response normalization:

    choice.message.content    → response.text
    choice.message.tool_calls → response.tool_calls
    choice.finish_reason      → response.finish_reason
    usage.{prompt,completion,total}_tokens → LLMUsage
"""

from __future__ import annotations

import json
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

logger = logging.getLogger(__name__)

_WIRE_EXTRA_KEYS = frozenset(
    {
        "frequency_penalty",
        "parallel_tool_calls",
        "presence_penalty",
        "seed",
        "tool_choice",
    }
)


def _safe_header(value: Any) -> str:
    return str(value or "").replace("\r", "").replace("\n", "")[:256]


class OpenAILLMAdapter(LLMAdapter):
    """Adapter over `openai.AsyncOpenAI`."""

    provider_name = "openai"
    default_model = "gpt-4.1"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        organization: str = "",
        default_model: str = "",
        timeout_seconds: float | None = None,
        provider_name: str = "",
    ) -> None:
        if not api_key:
            raise AdapterNotConfigured("openai adapter requires DEVAI_OPENAI_API_KEY")
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise AdapterNotInstalled("openai adapter requires `pip install openai` — falling back to Noop") from e

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if organization:
            kwargs["organization"] = organization
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        self._client = AsyncOpenAI(**kwargs)
        if default_model:
            self.default_model = default_model
        if provider_name:
            # OpenAI-compatible backends (groq, openrouter, gateway) reuse
            # this adapter; the name keeps telemetry/metrics honest.
            self.provider_name = provider_name

    async def list_models(self) -> list[dict[str, str]]:
        """`GET /models` — identical wire shape for OpenAI/Groq/OpenRouter."""
        try:
            out: list[dict[str, str]] = []
            async for m in self._client.models.list():
                mid = getattr(m, "id", "") or ""
                if mid:
                    out.append({"id": mid, "display_name": getattr(m, "display_name", "") or mid})
            return sorted(out, key=lambda x: x["id"])
        except Exception:  # noqa: BLE001
            logger.warning("%s adapter list_models failed", self.provider_name, exc_info=True)
            return []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        kwargs = self._build_kwargs(request)
        started = time.monotonic()
        try:
            raw = await self._client.chat.completions.create(**kwargs)
        except Exception:
            logger.exception("openai adapter generate() failed")
            raise

        latency_ms = (time.monotonic() - started) * 1000.0
        return self._normalize(raw, latency_ms)

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        mdl = model or "text-embedding-3-small"
        result = await self._client.embeddings.create(model=mdl, input=texts)
        return [list(d.embedding) for d in result.data]

    async def health_check(self) -> dict[str, Any]:
        try:
            await self._client.chat.completions.create(
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
        wire_messages: list[dict[str, Any]] = []
        if request.system:
            wire_messages.append({"role": "system", "content": request.system})

        for m in request.messages:
            if m.role == LLMRole.SYSTEM:
                # Caller passed system as a message — fold it in
                wire_messages.append({"role": "system", "content": m.content})
            elif m.role == LLMRole.TOOL:
                wire_messages.append(
                    {
                        "role": "tool",
                        "content": m.content,
                        "tool_call_id": m.tool_call_id,
                    }
                )
            else:
                wire_messages.append({"role": m.role.value, "content": m.content})

        kwargs: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": wire_messages,
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.stop_sequences:
            kwargs["stop"] = list(request.stop_sequences)
        if request.response_format:
            kwargs["response_format"] = dict(request.response_format)
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                    },
                }
                for t in request.tools
            ]
        extra = request.extra or {}
        for key in _WIRE_EXTRA_KEYS:
            if key in extra:
                kwargs[key] = extra[key]
        if self.provider_name == "gateway":
            headers = {
                "x-devai-tenant-id": _safe_header(extra.get("tenant_id")),
                "x-devai-user-id": _safe_header(extra.get("user_id")),
                "x-devai-run-id": _safe_header(extra.get("run_id")),
                "x-devai-agent": _safe_header(extra.get("agent")),
            }
            headers = {key: value for key, value in headers.items() if value}
            if headers:
                kwargs["extra_headers"] = headers
        return kwargs

    def _normalize(self, raw: Any, latency_ms: float) -> LLMResponse:
        choices = getattr(raw, "choices", []) or []
        if not choices:
            return LLMResponse(
                text="",
                usage=LLMUsage(),
                finish_reason="empty",
                model=str(getattr(raw, "model", "") or ""),
                provider=self.provider_name,
                request_id=str(getattr(raw, "id", "") or ""),
                latency_ms=latency_ms,
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        text = (getattr(message, "content", None) or "") if message is not None else ""

        tool_calls: list[ToolCall] = []
        raw_calls = getattr(message, "tool_calls", None) if message is not None else None
        for tc in raw_calls or []:
            func = getattr(tc, "function", None)
            if func is None:
                continue
            args_raw = getattr(func, "arguments", "") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args_raw}
            tool_calls.append(
                ToolCall(
                    id=str(getattr(tc, "id", "") or ""),
                    name=str(getattr(func, "name", "") or ""),
                    arguments=args,
                )
            )

        usage_raw = getattr(raw, "usage", None)
        usage = LLMUsage()
        if usage_raw is not None:
            usage.prompt_tokens = int(getattr(usage_raw, "prompt_tokens", 0) or 0)
            usage.completion_tokens = int(getattr(usage_raw, "completion_tokens", 0) or 0)
            usage.total_tokens = int(
                getattr(usage_raw, "total_tokens", usage.prompt_tokens + usage.completion_tokens) or 0
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            model=str(getattr(raw, "model", "") or ""),
            provider=self.provider_name,
            request_id=str(getattr(raw, "id", "") or ""),
            latency_ms=latency_ms,
        )


__all__ = ["OpenAILLMAdapter"]
