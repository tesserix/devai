"""Dynamic, capability-aware LLM routing.

The one place that knows, per provider, the best model for each capability
tier — and that detects which providers are actually CONNECTED for a given
settings/overlay (i.e. have credentials that build a non-noop adapter).

Used by ``create_role_llm`` so each agent runs on the tier-appropriate model
of every provider in the configured primary/fallback order. An Anthropic
model preference therefore becomes the configured Gemini tier model while
Vertex is primary, then returns to the exact Claude model on Anthropic fallback.

The configured ``llm_model_<role>`` stays the source of truth for the TIER
(via :func:`tier_for_model`) and for the exact id when its own provider is
connected. See docs/plans/dynamic-llm-capabilities/IMPLEMENTATION-PLAN.md.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.llm.model_policy import normalize_model

logger = logging.getLogger(__name__)

TIERS = ("light", "standard", "heavy", "frontier")

# provider × tier → the model to use. Adding a provider or re-pointing a tier
# is a one-line edit here; nothing else changes. Values are native ids that the
# pricing table (analytics/pricing.py) already knows. ``gateway`` routes claude
# aliases on Vertex, so it mirrors anthropic. ``openrouter`` is intentionally
# absent → model_for() returns "" → it uses its configured default model.
PROVIDER_TIER_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "light": "claude-haiku-4-5-20251001",
        "standard": "claude-sonnet-4-6",
        "heavy": "claude-opus-4-8",
        "frontier": "claude-opus-4-8",
    },
    "gateway": {
        "light": "claude-haiku-4-5-20251001",
        "standard": "claude-sonnet-4-6",
        "heavy": "claude-opus-4-8",
        "frontier": "claude-opus-4-8",
    },
    "openai": {
        "light": "gpt-4.1-mini",
        "standard": "gpt-4.1",
        "heavy": "o3",
        "frontier": "o3",
    },
    "vertex_gemini": {
        "light": "gemini-2.5-flash",
        "standard": "gemini-2.5-pro",
        "heavy": "gemini-2.5-pro",
        "frontier": "gemini-2.5-pro",
    },
    "groq": {
        "light": "llama-3.3-70b-versatile",
        "standard": "llama-3.3-70b-versatile",
        "heavy": "llama-3.3-70b-versatile",
        "frontier": "llama-3.3-70b-versatile",
    },
}

DEFAULT_TIER = "standard"


def tier_for_model(model: str) -> str:
    """Infer the capability tier a model id represents.

    The configured ``llm_model_<role>`` thus IS the role's tier — no separate
    role→tier table to keep in sync.
    """
    m = (model or "").strip().lower()
    if not m:
        return DEFAULT_TIER
    if "opus" in m or m.startswith(("o1", "o3", "o4")) or "gpt-5" in m:
        return "heavy"
    if "haiku" in m or "-mini" in m or "nano" in m or "flash-lite" in m or "flash-8b" in m:
        return "light"
    return DEFAULT_TIER


def natural_provider(model: str) -> str:
    """The provider whose native catalog a model id belongs to ('' if unknown)."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if m.startswith("gemini"):
        return "vertex_gemini"
    if m.startswith(("llama", "mixtral", "gemma", "qwen", "deepseek", "mistral")):
        return "groq"
    return ""


def model_for(provider: str, tier: str) -> str:
    """The model ``provider`` should use for ``tier``; '' → provider default."""
    return PROVIDER_TIER_MODELS.get((provider or "").lower(), {}).get((tier or DEFAULT_TIER).lower(), "")


def model_for_provider(settings: Any, provider: str, requested_model: str) -> str:
    """Map a requested model onto ``provider`` without changing provider order."""
    from devai.adapters.llm.model_policy import provider_serves

    requested = normalize_model(requested_model)
    tier = tier_for_model(requested)
    raw = str(getattr(settings, f"llm_tier_{tier}", "") or "")
    configured_provider, separator, configured_model = raw.partition(":")
    if separator and configured_provider.strip().lower() == provider.lower() and configured_model.strip():
        return normalize_model(configured_model.strip())
    if requested and provider_serves(provider, requested):
        return requested
    return model_for(provider, tier)


def ordered_providers(settings: Any, prefer: str | None = None) -> list[str]:
    """Provider preference order for this settings/overlay (deduped, known).

    The configured ``llm_provider`` is always first, followed by the explicit
    ``llm_fallback_provider`` order. A role's natural provider is only a hint
    when no provider order exists; model preferences must never silently move
    a fallback ahead of the product's primary. ``llm_role_chain_provider`` is
    retained only for deployments that do not already require Agent Gateway.
    """
    from devai.adapters.llm.factory import llm_registry

    authorized_raw = getattr(settings, "llm_authorized_providers", None)
    if isinstance(authorized_raw, str):
        authorized = [p.strip().lower() for p in authorized_raw.split(",") if p.strip()]
    elif authorized_raw is not None:
        authorized = [str(p).strip().lower() for p in authorized_raw if str(p).strip()]
    else:
        authorized = []

    primary = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    fallbacks = [
        name.strip().lower()
        for name in str(getattr(settings, "llm_fallback_provider", "") or "").split(",")
        if name.strip()
    ]
    order = [primary, *fallbacks]
    if not any(provider and provider != "noop" for provider in order) and prefer:
        order.append(prefer.strip().lower())
    if not bool(getattr(settings, "llm_gateway_required", False)):
        gateway = str(getattr(settings, "llm_role_chain_provider", "") or "").strip().lower()
        if gateway:
            order.append(gateway)
    if authorized_raw is not None:
        order += authorized
        allowed = set(authorized)
        order = [p for p in order if p in allowed]
    out: list[str] = []
    seen: set[str] = set()
    for p in order:
        if p and p not in seen and p != "noop" and llm_registry.has(p):
            seen.add(p)
            out.append(p)
    return out


def connected_providers(settings: Any, prefer: str | None = None) -> list[str]:
    """The subset of :func:`ordered_providers` that actually build a non-noop
    adapter for this settings/overlay — i.e. the providers with credentials.
    This is the dynamic "what is the app connected to right now"."""
    from devai.adapters.llm.factory import create_llm_adapter

    live: list[str] = []
    for p in ordered_providers(settings, prefer):
        try:
            if create_llm_adapter(settings, provider=p).provider_name != "noop":
                live.append(p)
        except Exception:  # noqa: BLE001 — a build blip ≠ connected
            logger.debug("connectivity probe for %s failed", p, exc_info=True)
    return live


def describe_capabilities(settings: Any, roles: list[str] | None = None) -> dict[str, Any]:
    """Introspection: what's connected + how each role resolves. Read-only,
    never includes credentials — the basis of the capabilities API/UI so the
    system can SHOW that it knows how it's configured."""
    connected = connected_providers(settings)
    role_list = roles or [
        "dev_ui",
        "dev_api",
        "review",
        "planning",
        "utility",
        "boardroom_panel",
        "boardroom_moderator",
    ]
    resolved: dict[str, dict[str, str]] = {}
    for role in role_list:
        model = normalize_model(str(getattr(settings, f"llm_model_{role}", "") or ""))
        prov, mdl = resolve_primary(settings, role)
        resolved[role] = {"tier": tier_for_model(model), "provider": prov, "model": mdl}
    return {
        "connected": connected,
        "primary": connected[0] if connected else "",
        "gateway_required": bool(getattr(settings, "llm_gateway_required", False)),
        "roles": resolved,
    }


def resolve_primary(settings: Any, role: str) -> tuple[str, str]:
    """The (provider, model) a role resolves to on the FIRST connected provider.

    ('', '') when nothing is connected. The configured ``llm_model_<role>`` is
    honored on its own provider; otherwise the first connected provider's
    tier-appropriate model.
    """
    model = normalize_model(str(getattr(settings, f"llm_model_{role}", "") or ""))
    tier = tier_for_model(model)
    live = connected_providers(settings, prefer=natural_provider(model))
    if not live:
        return ("", model)
    primary = live[0]
    return (primary, model_for_provider(settings, primary, model) or model_for(primary, tier) or model)


__all__ = [
    "DEFAULT_TIER",
    "PROVIDER_TIER_MODELS",
    "TIERS",
    "connected_providers",
    "describe_capabilities",
    "model_for",
    "model_for_provider",
    "natural_provider",
    "ordered_providers",
    "resolve_primary",
    "tier_for_model",
]
