"""Model-id policy — keep every LLM call on a model the resolved provider
can actually serve.

Applied once, at the :class:`InstrumentedLLMAdapter` chokepoint, so the two
guarantees below hold for EVERY call path (role chains, spec agents, chat,
SRE, boardroom) without touching any call site:

1. **No Fable models.** Any ``claude-fable-*`` id — from config, a per-user
   overlay, or a spec YAML — is remapped to ``claude-opus-4-8`` (the 4.8
   model). Nothing in DevAI runs on Fable.

2. **Provider/model fit.** When a pinned model belongs to a KNOWN family the
   resolved provider can't serve (a ``claude-*`` id reaching an OpenAI
   adapter — the exact cause of "every seat absent → runbook → needs human"),
   the model is cleared so the provider's OWN default applies, instead of the
   provider 4xx-ing on an unknown id. This is the "proper fallback": a user
   on OpenAI (Codex) keeps running on OpenAI's default model when an agent is
   pinned to a Claude id their account can't serve. Unknown/custom ids are
   left untouched (gateway aliases, fine-tunes, future models) — we only
   override on a CONFIDENT mismatch, never a guess.
"""

from __future__ import annotations

# The 4.8 model every retired Fable id falls back to.
FABLE_FALLBACK_MODEL = "claude-opus-4-8"

# provider_name → the model family it serves natively.
_PROVIDER_FAMILY = {
    "anthropic": "claude",
    "openai": "openai",
    "groq": "groq",
    "vertex_gemini": "gemini",
}

# Providers that route arbitrary model aliases server-side to any backend —
# their model ids are gateway-defined, so never override them.
_ALIAS_ROUTERS = ("gateway", "openrouter")


def normalize_model(model: str) -> str:
    """Remap retired/forbidden ids. Today: any ``claude-fable-*`` → 4.8."""
    m = (model or "").strip()
    if m.lower().startswith("claude-fable"):
        return FABLE_FALLBACK_MODEL
    return m


def model_family(model: str) -> str:
    """Best-effort family of a model id; ``""`` when unknown (never override)."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    if m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-embedding", "davinci", "babbage")):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("llama", "mixtral", "gemma", "qwen", "deepseek", "mistral")):
        return "groq"
    return ""


def provider_serves(provider_name: str, model: str) -> bool:
    """True when ``provider_name`` can serve ``model``.

    Empty model (no pin), unknown families, and alias-routing providers
    (gateway) are all considered served — we only return False on a confident,
    known cross-family mismatch.
    """
    if not model:
        return True
    fam = model_family(model)
    if not fam:
        return True  # unknown/custom id — assume the provider knows it
    # provider_name may be a fallback-chain string like "anthropic→gateway".
    parts = (provider_name or "").lower().replace("→", " ").replace(",", " ").split()
    return any(part in _ALIAS_ROUTERS or _PROVIDER_FAMILY.get(part) == fam for part in parts)


def coerce_model(provider_name: str, model: str) -> str:
    """The model to actually send for ``provider_name``: fable-normalized, and
    cleared to ``""`` (→ the provider's default) when it can't serve it."""
    norm = normalize_model(model)
    if not norm:
        return ""
    return norm if provider_serves(provider_name, norm) else ""


__all__ = [
    "FABLE_FALLBACK_MODEL",
    "coerce_model",
    "model_family",
    "normalize_model",
    "provider_serves",
]
