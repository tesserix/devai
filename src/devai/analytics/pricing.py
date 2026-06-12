"""Model pricing → USD cost for token usage.

The analytics layer records ``llm_cost_usd`` per agent execution. This module
turns (provider, model, tokens_in, tokens_out) into that dollar figure using a
published rate card. Prices are USD per 1,000,000 tokens.

Matching is forgiving: a versioned/suffixed model id (``claude-opus-4-8``,
``gemini-2.5-flash``, ``gpt-4.1-2025-...``) resolves to its base rate via
longest-prefix match, so new point-releases don't silently fall to $0. An
unknown model resolves to a conservative default and is flagged so the rate
card can show "estimated".

Rates are deliberately data, not code — update the table as vendors change
pricing. Figures are list prices as of mid-2026; treat as estimates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rate:
    """USD per 1M input / output tokens."""

    input: float
    output: float


# Keyed by a model-id prefix. Longest matching prefix wins, so
# ``claude-opus-4`` is tried before ``claude``. USD / 1M tokens.
_RATES: dict[str, Rate] = {
    # ── Anthropic / Claude ──
    "claude-fable-5": Rate(5.0, 25.0),
    "claude-opus-4": Rate(5.0, 25.0),
    "claude-sonnet-4": Rate(3.0, 15.0),
    "claude-haiku-4": Rate(0.80, 4.0),
    "claude-3-5-haiku": Rate(0.80, 4.0),
    "claude-3-5-sonnet": Rate(3.0, 15.0),
    "claude": Rate(3.0, 15.0),  # family fallback
    # ── Google Gemini (Vertex + direct) ──
    "gemini-3.1-pro": Rate(1.25, 10.0),
    "gemini-3.5-flash": Rate(0.30, 2.50),
    "gemini-3-flash": Rate(0.30, 2.50),
    "gemini-3.1-flash-lite": Rate(0.10, 0.40),
    "gemini-2.5-pro": Rate(1.25, 10.0),
    "gemini-2.5-flash-lite": Rate(0.10, 0.40),
    "gemini-2.5-flash": Rate(0.30, 2.50),
    "gemini-2.0-flash-lite": Rate(0.075, 0.30),
    "gemini-2.0-flash": Rate(0.10, 0.40),
    "gemini-1.5-pro": Rate(1.25, 5.0),
    "gemini": Rate(0.30, 2.50),  # family fallback
    "text-embedding": Rate(0.025, 0.0),
    "gemini-embedding": Rate(0.025, 0.0),
    # ── OpenAI ──
    "o3": Rate(2.0, 8.0),
    "o4-mini": Rate(1.10, 4.40),
    "gpt-4.1-mini": Rate(0.40, 1.60),
    "gpt-4.1-nano": Rate(0.10, 0.40),
    "gpt-4.1": Rate(2.0, 8.0),
    "gpt-4o-mini": Rate(0.15, 0.60),
    "gpt-4o": Rate(2.50, 10.0),
    # ── Groq (Llama / Mixtral hosted) ──
    "llama-3.3-70b": Rate(0.59, 0.79),
    "llama-3.1-8b": Rate(0.05, 0.08),
    "llama": Rate(0.59, 0.79),
    "mixtral": Rate(0.24, 0.24),
    # ── OpenRouter common open models ──
    "meta-llama/llama-3.3-70b": Rate(0.12, 0.30),
    "mistralai/": Rate(0.20, 0.60),
    "deepseek/": Rate(0.40, 0.89),
    # ── Noop / unknown ──
    "noop": Rate(0.0, 0.0),
}

# Used when nothing matches — conservative mid-tier so cost is never silently 0.
_DEFAULT = Rate(1.0, 3.0)


def lookup_rate(model: str) -> tuple[Rate, bool]:
    """(Rate, exact) for a model id. ``exact`` is False when the default was used."""
    m = (model or "").strip().lower()
    if not m:
        return _DEFAULT, False
    best = ""
    for prefix in _RATES:
        if m.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    if best:
        return _RATES[best], True
    # Substring pass for "vendor/model" ids that don't prefix-match cleanly.
    for prefix, rate in _RATES.items():
        if prefix in m:
            return rate, True
    return _DEFAULT, False


def estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    """USD for a single call. Provider is advisory — model id drives the rate."""
    rate, _ = lookup_rate(model)
    cost = (max(0, tokens_in) / 1_000_000) * rate.input + (max(0, tokens_out) / 1_000_000) * rate.output
    return round(cost, 6)


def rate_card() -> list[dict[str, object]]:
    """The published rates, for the analytics 'how cost is computed' panel."""
    return [
        {"model_prefix": prefix, "input_per_1m_usd": r.input, "output_per_1m_usd": r.output}
        for prefix, r in sorted(_RATES.items())
        if prefix != "noop"
    ]


__all__ = ["Rate", "estimate_cost", "lookup_rate", "rate_card"]
