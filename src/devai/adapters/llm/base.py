"""LLMAdapter ABC + canonical request/response types.

Every LLM backend speaks the same shape: take an `LLMRequest`, return
an `LLMResponse`. Tool-calling is first-class because every modern LLM
supports it and DevAI's specializations declare an `allowed_tools` list.

The types deliberately don't expose vendor-specific fields. When a
backend has a useful extra (Anthropic's `cache_control`, OpenAI's
`logprobs`), it goes in `LLMRequest.extra` / `LLMResponse.extra` as
free-form metadata — opt-in for callers that need it, ignored by
callers that don't.
"""

from __future__ import annotations

import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

from devai.adapters.base import Adapter


class LLMRole(str, Enum):
    """Standard chat roles. `tool` is the response from a tool call."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    @classmethod
    def parse(cls, value: Any, *, default: "LLMRole | None" = None) -> "LLMRole":
        if value is None or value == "":
            return default or cls.USER
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError:
            return default or cls.USER


@dataclass(slots=True)
class LLMMessage:
    """One turn in a chat conversation."""

    role: LLMRole
    content: str
    name: str = ""  # tool name for role=TOOL; sender name for others
    tool_call_id: str = ""  # links a TOOL message to the ToolCall that produced it

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        return out


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """Tool declaration the LLM can call.

    Mirrors the OpenAI / Anthropic shape. `parameters` is a JSON Schema
    object — both SDKs accept this directly.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(slots=True)
class ToolCall:
    """One tool invocation the model produced.

    `id` is the provider-issued call id; the caller passes it back when
    sending the TOOL message containing the result.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}


@dataclass(slots=True)
class LLMRequest:
    """A single LLM invocation."""

    system: str = ""
    messages: list[LLMMessage] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    model: str = ""  # empty → adapter's default
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] = field(default_factory=list)
    response_format: dict[str, Any] | None = None  # {"type": "json_object"} etc.
    stream: bool = False
    # Per-call adapter-specific knobs (cache_control, logprobs, …).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMUsage:
    """Token accounting returned by every provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0  # populated where supported (Anthropic prompt cache)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }


@dataclass(slots=True)
class LLMResponse:
    """Normalized result.

    `text` is always populated (empty string when the response was
    tool-calls-only). `tool_calls` is the list of calls the model wants
    executed — the caller dispatches them and feeds the results back as
    TOOL messages on the next request.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str = ""  # stop | length | tool_use | content_filter | error
    model: str = ""
    provider: str = ""
    request_id: str = ""
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "model": self.model,
            "provider": self.provider,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
        }


class LLMAdapter(Adapter):
    """The minimum surface every LLM backend implements.

    Four operations:
      - `generate(request)`           — one-shot completion
      - `stream(request)`             — async iterator of partial responses
      - `embed(texts)`                — embedding(s) for the supplied text(s)
      - `close` + `health_check`      — inherited Adapter contract

    Backends that don't support streaming should implement `stream` as
    a yield-once wrapper around `generate`. Backends without embedding
    raise `NotImplementedError` from `embed` and downstream code falls
    back to a different provider.
    """

    provider_name: str = "abstract"
    default_model: str = ""

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        """Default fallback: yield the single non-streamed response."""
        response = await self.generate(request)
        yield response

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        """Default: not supported. Override in backends that do."""
        raise NotImplementedError(f"{self.provider_name} adapter does not implement embeddings")

    # ── Adapter contract ──────────────────────────────────────────────

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "no health check implemented"}


# ─── Convenience builders ───────────────────────────────────────────


def make_request(
    *,
    system: str = "",
    user: str = "",
    messages: list[LLMMessage] | None = None,
    tools: list[ToolSpec] | None = None,
    model: str = "",
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
) -> LLMRequest:
    """Build an LLMRequest from common shorthand patterns.

    Either pass `user=...` for a one-shot user prompt, OR pass
    `messages=[...]` for full multi-turn control. Both at once is
    accepted — `user` is appended after `messages`.
    """
    msgs: list[LLMMessage] = list(messages or [])
    if user:
        msgs.append(LLMMessage(role=LLMRole.USER, content=user))
    return LLMRequest(
        system=system,
        messages=msgs,
        tools=list(tools or []),
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
    )


def _now_ms() -> float:
    return time.monotonic() * 1000.0


__all__ = [
    "LLMAdapter",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "LLMUsage",
    "ToolCall",
    "ToolSpec",
    "make_request",
]
