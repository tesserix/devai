"""LLM adapter family — pluggable LLM backends behind one interface.

Today: openai + anthropic + noop. The factory accepts a `provider`
string and returns the matching adapter; specializations whose YAML
declares `llm_provider:` get the right one without business logic
caring which vendor SDK runs underneath.

Public surface:

    LLMAdapter         — the ABC every backend implements
    LLMMessage         — one chat-style message
    LLMRequest         — full request envelope (system, messages, tools, ...)
    LLMResponse        — normalized response (text, tool_calls, usage, ...)
    ToolSpec           — one tool the LLM may call
    ToolCall           — one tool invocation in a response
    create_llm_adapter(settings) — factory; reads DEVAI_LLM_PROVIDER

Backends:

    noop       — returns canned responses; for tests and disabled mode
    anthropic  — Claude via anthropic SDK
    openai     — gpt-*, o-series via openai SDK

Backend SDKs are lazy-imported. A backend you don't configure never
loads its SDK. Missing optional deps degrade to Noop rather than crash.

Planned extensions (slot in with no churn elsewhere):
    groq | gemini | nemoclaw | codex
"""

from devai.adapters.llm.base import (
    LLMAdapter,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    ToolCall,
    ToolSpec,
)
from devai.adapters.llm.factory import (
    KNOWN_PROVIDERS,
    create_llm_adapter,
    create_llm_chain,
    create_role_llm,
    llm_registry,
    role_llm_or,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "LLMAdapter",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "ToolCall",
    "ToolSpec",
    "create_llm_adapter",
    "create_llm_chain",
    "create_role_llm",
    "role_llm_or",
    "llm_registry",
]
