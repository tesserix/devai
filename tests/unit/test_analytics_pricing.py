"""Pricing resolution + cost estimation for all integrated LLMs."""

from devai.analytics.pricing import estimate_cost, lookup_rate, rate_card


def test_known_models_resolve_exact():
    for model in (
        "claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
        "gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-pro",
        "gpt-4.1", "o3", "llama-3.3-70b-versatile",
    ):
        _, exact = lookup_rate(model)
        assert exact, f"{model} should resolve to a real rate"


def test_versioned_id_resolves_via_prefix():
    # A dated suffix must still resolve to the base rate, not the default.
    _, exact = lookup_rate("gpt-4.1-2025-08-01")
    assert exact


def test_unknown_model_uses_default_not_zero():
    rate, exact = lookup_rate("some-brand-new-model-x")
    assert not exact
    assert rate.input > 0 and rate.output > 0  # never silently $0


def test_estimate_cost_math():
    # claude-sonnet-4: $3/1M in, $15/1M out. 1M in + 1M out = $18.
    assert estimate_cost("anthropic", "claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    # gemini flash-lite is cheap.
    assert estimate_cost("vertex_gemini", "gemini-3.1-flash-lite", 1_000_000, 0) == 0.10


def test_rate_card_lists_all_families():
    card = rate_card()
    prefixes = {r["model_prefix"] for r in card}
    assert any(p.startswith("claude") for p in prefixes)
    assert any(p.startswith("gemini") for p in prefixes)
    assert any(p in ("o3", "gpt-4.1") for p in prefixes)
