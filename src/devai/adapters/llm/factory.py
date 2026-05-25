"""Factory — pick the right LLM backend at runtime.

Reads `settings.llm_provider` (env: `DEVAI_LLM_PROVIDER`) and returns
one of:

    noop       → NoopLLMAdapter
    anthropic  → AnthropicLLMAdapter
    openai     → OpenAILLMAdapter

Graceful degradation rules (identical to the memory adapter family):
  - Unknown provider → log + Noop
  - Missing SDK      → catch `AdapterNotInstalled` → Noop
  - Missing config   → catch `AdapterNotConfigured` → Noop
  - Any other build error → log + Noop

Future providers (groq, gemini, nemoclaw, codex) plug in by adding one
`_build_*` function + one `memory_registry.register(...)` line. No
churn elsewhere — this is the adapter pattern's whole point.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import (
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)
from devai.adapters.llm.base import LLMAdapter
from devai.adapters.llm.noop import NoopLLMAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "anthropic", "openai")


def _build_noop(settings: Any) -> LLMAdapter:
    return NoopLLMAdapter(canned_text=getattr(settings, "llm_noop_canned_text", "[noop response]"))


def _build_anthropic(settings: Any) -> LLMAdapter:
    from devai.adapters.llm.anthropic_adapter import AnthropicLLMAdapter

    api_key = getattr(settings, "anthropic_api_key", "") or ""
    if not api_key:
        raise AdapterNotConfigured("anthropic adapter requires DEVAI_ANTHROPIC_API_KEY")
    return AnthropicLLMAdapter(
        api_key=api_key,
        base_url=getattr(settings, "anthropic_base_url", "") or "",
        default_model=getattr(settings, "claude_model", "") or "",
        default_max_tokens=int(getattr(settings, "claude_max_tokens", 0) or 0) or None,
    )


def _build_openai(settings: Any) -> LLMAdapter:
    from devai.adapters.llm.openai_adapter import OpenAILLMAdapter

    api_key = getattr(settings, "openai_api_key", "") or ""
    if not api_key:
        raise AdapterNotConfigured("openai adapter requires DEVAI_OPENAI_API_KEY")
    return OpenAILLMAdapter(
        api_key=api_key,
        base_url=getattr(settings, "openai_base_url", "") or "",
        organization=getattr(settings, "openai_organization", "") or "",
        default_model=getattr(settings, "openai_model", "") or "",
    )


llm_registry: AdapterRegistry[LLMAdapter] = AdapterRegistry("llm")
llm_registry.register("noop", _build_noop)
llm_registry.register("anthropic", _build_anthropic)
llm_registry.register("openai", _build_openai)


def create_llm_adapter(settings: Any, *, provider: str | None = None) -> LLMAdapter:
    """Resolve `settings.llm_provider` (or explicit override) to an adapter.

    Never raises — on any error returns a NoopLLMAdapter and logs the
    reason. The provider arg lets specializations override the global
    default per-role: a spec YAML can declare `llm_provider: openai`
    and the runner passes that name in here.
    """
    chosen = (provider or getattr(settings, "llm_provider", "noop") or "noop").lower()
    if not llm_registry.has(chosen):
        logger.warning(
            "llm_provider=%r is unknown (known: %s) — using Noop",
            chosen,
            ", ".join(llm_registry.known()),
        )
        return NoopLLMAdapter()

    try:
        adapter = llm_registry.resolve(chosen, settings)
        logger.info("LLMAdapter active: %s (model=%s)", adapter.provider_name, adapter.default_model)
        return adapter
    except AdapterNotInstalled as e:
        logger.warning("llm_provider=%s: %s — using Noop", chosen, e)
        return NoopLLMAdapter()
    except AdapterNotConfigured as e:
        logger.warning("llm_provider=%s: %s — using Noop", chosen, e)
        return NoopLLMAdapter()
    except AdapterError as e:
        logger.warning("llm_provider=%s failed to build (%s) — using Noop", chosen, e)
        return NoopLLMAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("llm_provider=%s crashed during build — using Noop", chosen)
        return NoopLLMAdapter()


__all__ = ["KNOWN_PROVIDERS", "create_llm_adapter", "llm_registry"]
