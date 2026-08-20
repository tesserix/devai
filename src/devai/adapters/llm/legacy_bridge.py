"""Request-scoped LLM bridge for legacy provider-specific agents."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from devai.adapters.llm.base import LLMAdapter

_current_llm: contextvars.ContextVar[LLMAdapter | None] = contextvars.ContextVar(
    "devai_legacy_llm",
    default=None,
)


@contextmanager
def bind_legacy_llm(adapter: LLMAdapter | None) -> Iterator[None]:
    token = _current_llm.set(adapter)
    try:
        yield
    finally:
        _current_llm.reset(token)


def current_legacy_llm() -> LLMAdapter | None:
    return _current_llm.get()


__all__ = ["bind_legacy_llm", "current_legacy_llm"]
