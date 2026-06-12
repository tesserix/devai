"""Factory — pick the right LLM backend at runtime.

Reads `settings.llm_provider` (env: `DEVAI_LLM_PROVIDER`) and returns
one of:

    noop       → NoopLLMAdapter
    anthropic  → AnthropicLLMAdapter
    openai     → OpenAILLMAdapter
    gateway    → OpenAILLMAdapter pointed at the agentgateway (model
                 routing + provider credentials live gateway-side)
    vertex_gemini → VertexGeminiLLMAdapter (REST + ADC/Workload Identity,
                 VPC-private via the PSC-pinned aiplatform DNS zone)

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

KNOWN_PROVIDERS = ("noop", "anthropic", "openai", "gateway", "vertex_gemini")


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


def _build_gateway(settings: Any) -> LLMAdapter:
    """The solo.io agentgateway as the single LLM egress.

    The gateway exposes an OpenAI-compatible endpoint and routes each
    model alias to whatever backend its config names (Vertex Gemini /
    Vertex Claude / Anthropic direct / OpenAI / self-hosted). DevAI
    stays provider-agnostic: this builder is just the OpenAI adapter
    pointed at the gateway service, so swapping or adding backends is
    a gateway config change — never a DevAI change.
    """
    from devai.adapters.llm.openai_adapter import OpenAILLMAdapter

    base_url = getattr(settings, "llm_gateway_base_url", "") or ""
    if not base_url:
        raise AdapterNotConfigured("gateway adapter requires DEVAI_LLM_GATEWAY_BASE_URL")
    return OpenAILLMAdapter(
        # The OpenAI SDK insists on a non-empty key; the gateway decides
        # whether to enforce it.
        api_key=getattr(settings, "llm_gateway_api_key", "") or "not-needed",
        base_url=base_url,
        default_model=getattr(settings, "llm_gateway_model", "") or "",
    )


def _build_vertex_gemini(settings: Any) -> LLMAdapter:
    """Vertex AI Gemini over REST + ADC (Workload Identity in-cluster).

    Keyless and VPC-private: auth comes from the pod's GSA and traffic
    rides the PSC endpoint via the pinned aiplatform DNS zone.
    """
    from devai.adapters.llm.vertex_gemini import VertexGeminiLLMAdapter

    return VertexGeminiLLMAdapter(
        project=getattr(settings, "vertex_project", "") or "",
        location=getattr(settings, "vertex_location", "") or "global",
        default_model=getattr(settings, "vertex_gemini_model", "") or "",
        embedding_model=getattr(settings, "vertex_embedding_model", "") or "",
        base_url=getattr(settings, "vertex_base_url", "") or "",
        api_key=getattr(settings, "vertex_api_key", "") or "",
    )


llm_registry: AdapterRegistry[LLMAdapter] = AdapterRegistry("llm")
llm_registry.register("noop", _build_noop)
llm_registry.register("anthropic", _build_anthropic)
llm_registry.register("openai", _build_openai)
llm_registry.register("gateway", _build_gateway)
llm_registry.register("vertex_gemini", _build_vertex_gemini)


LLM_TIERS = ("light", "standard", "heavy", "frontier")

# Specialization YAMLs declare providers by friendly names (the
# LLMProvider enum: claude/openai/codex/groq/gemini/nemoclaw/auto).
# Map them onto registered factory backends; None = "no opinion, use
# whatever adapter the run already has" (auto, unknown, or a backend
# that has no registered adapter yet).
_SPEC_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "openai": "openai",
    "codex": "openai",
    "gemini": "vertex_gemini",  # Vertex is the live Gemini path (PSC + WI)
    "vertex_gemini": "vertex_gemini",
    "gateway": "gateway",
    "noop": "noop",
}


def resolve_spec_provider(name: str) -> str | None:
    """Factory backend for a specialization's ``llm_provider``, or None.

    Unknown/auto/unregistered names return None rather than noop so a
    spec with an aspirational provider (groq, nemoclaw) runs on the
    default adapter instead of silently answering with canned text.
    """
    mapped = _SPEC_PROVIDER_ALIASES.get((name or "").strip().lower())
    if mapped and llm_registry.has(mapped):
        return mapped
    return None


def resolve_llm_tier(settings: Any, tier: str) -> tuple[str, str]:
    """Map a cost tier to a ``(provider, model)`` pair.

    Tiers are configured as ``provider:model`` strings
    (``DEVAI_LLM_TIER_LIGHT`` …), so re-pointing a whole cost class —
    e.g. light → a cheaper Gemini once it's model-enabled — is an env
    change, never a code or spec change. Unknown/unset tiers fall back
    to the global default provider with its default model: callers can
    always ask for a tier and get something that works.
    """
    name = (tier or "").strip().lower()
    raw = getattr(settings, f"llm_tier_{name}", "") if name in LLM_TIERS else ""
    if raw and ":" in raw:
        provider, _, model = raw.partition(":")
        provider, model = provider.strip(), model.strip()
        if llm_registry.has(provider.lower()):
            return provider.lower(), model
        logger.warning("llm tier %r names unknown provider %r — using global default", name, provider)
    elif raw:
        logger.warning("llm tier %r is %r (expected provider:model) — using global default", name, raw)
    return (getattr(settings, "llm_provider", "noop") or "noop").lower(), ""


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
        # Wrap every backend in the telemetry delegate so EVERY caller —
        # pipeline agents, chat, SRE, probes — emits per-call token/latency
        # metrics into the process-global sink. Free when telemetry is Noop.
        from devai.adapters.llm.instrumented import InstrumentedLLMAdapter

        return InstrumentedLLMAdapter(adapter)
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


__all__ = [
    "KNOWN_PROVIDERS",
    "LLM_TIERS",
    "create_llm_adapter",
    "llm_registry",
    "resolve_llm_tier",
    "resolve_spec_provider",
]
