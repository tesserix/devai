"""Dynamic, capability-aware LLM routing — capabilities module.

Covers: tier inference, natural-provider mapping, provider×tier model table,
connected-provider detection, and the per-role resolution that adapts to
whatever the tenant actually has connected.
"""

from __future__ import annotations

from devai.adapters.llm import capabilities as cap


class _S:
    llm_provider = "anthropic"
    llm_role_chain_provider = "gateway"
    llm_gateway_required = True
    llm_fallback_provider = "openai,vertex_gemini,groq"
    llm_model_dev_api = "claude-opus-4-8"
    llm_model_utility = "claude-haiku-4-5-20251001"
    llm_model_review = "claude-sonnet-4-6"


def _patch_live(monkeypatch, live: set[str]) -> None:
    """Make only `live` providers build a non-noop adapter."""
    from devai.adapters.llm import factory

    class _Fake:
        def __init__(self, name: str) -> None:
            self.provider_name = name
            self.default_model = ""

    def fake_create(settings, provider=None):
        name = (provider or getattr(settings, "llm_provider", "noop") or "noop").lower()
        return _Fake(name if name in live else "noop")

    monkeypatch.setattr(factory, "create_llm_adapter", fake_create)


def test_tier_for_model():
    assert cap.tier_for_model("claude-opus-4-8") == "heavy"
    assert cap.tier_for_model("o3") == "heavy"
    assert cap.tier_for_model("claude-sonnet-4-6") == "standard"
    assert cap.tier_for_model("gpt-4.1") == "standard"
    assert cap.tier_for_model("gemini-2.5-pro") == "standard"
    assert cap.tier_for_model("claude-haiku-4-5-20251001") == "light"
    assert cap.tier_for_model("gpt-4.1-mini") == "light"
    assert cap.tier_for_model("") == "standard"


def test_natural_provider():
    assert cap.natural_provider("claude-opus-4-8") == "anthropic"
    assert cap.natural_provider("o3") == "openai"
    assert cap.natural_provider("gpt-4.1") == "openai"
    assert cap.natural_provider("gemini-2.5-pro") == "vertex_gemini"
    assert cap.natural_provider("llama-3.3-70b-versatile") == "groq"
    assert cap.natural_provider("") == ""


def test_model_for():
    assert cap.model_for("openai", "heavy") == "o3"
    assert cap.model_for("anthropic", "light") == "claude-haiku-4-5-20251001"
    assert cap.model_for("groq", "heavy") == "llama-3.3-70b-versatile"
    assert cap.model_for("openrouter", "heavy") == ""  # not in map → provider default
    assert cap.model_for_provider(_S(), "openrouter", "claude-opus-4-8") == "claude-opus-4-8"


def test_ordered_providers_keeps_configured_primary_ahead_of_model_preference():
    assert cap.ordered_providers(_S(), prefer="anthropic") == [
        "anthropic",
        "openai",
        "vertex_gemini",
        "groq",
    ]
    assert cap.ordered_providers(_S(), prefer="openai")[0] == "anthropic"


def test_connected_providers_only_configured(monkeypatch):
    _patch_live(monkeypatch, {"anthropic", "groq"})
    assert cap.connected_providers(_S()) == ["anthropic", "groq"]


def test_resolve_primary_picks_tier_model_on_connected(monkeypatch):
    # Only OpenAI + Groq connected (no Anthropic). dev_api is heavy →
    # the first connected provider (openai) on the heavy tier = o3.
    _patch_live(monkeypatch, {"openai", "groq"})
    assert cap.resolve_primary(_S(), "dev_api") == ("openai", "o3")


def test_resolve_primary_honors_configured_id_on_own_provider(monkeypatch):
    _patch_live(monkeypatch, {"anthropic", "groq"})
    assert cap.resolve_primary(_S(), "dev_api") == ("anthropic", "claude-opus-4-8")


def test_resolve_primary_maps_foreign_role_model_to_primary_provider_tier(monkeypatch):
    class _VertexPrimary(_S):
        llm_provider = "vertex_gemini"
        llm_fallback_provider = "anthropic"
        llm_tier_heavy = "vertex_gemini:gemini-2.5-flash"

    _patch_live(monkeypatch, {"vertex_gemini", "anthropic"})
    assert cap.resolve_primary(_VertexPrimary(), "dev_api") == ("vertex_gemini", "gemini-2.5-flash")


def test_product_tier_model_overrides_agent_model_on_same_primary(monkeypatch):
    class _VertexPrimary(_S):
        llm_provider = "vertex_gemini"
        llm_fallback_provider = "anthropic"
        llm_model_dev_api = "gemini-2.5-pro"
        llm_tier_standard = "vertex_gemini:gemini-2.5-flash"

    _patch_live(monkeypatch, {"vertex_gemini", "anthropic"})
    assert cap.resolve_primary(_VertexPrimary(), "dev_api") == ("vertex_gemini", "gemini-2.5-flash")


def test_describe_capabilities(monkeypatch):
    _patch_live(monkeypatch, {"anthropic", "groq"})
    desc = cap.describe_capabilities(_S(), roles=["dev_api", "utility"])
    assert desc["connected"] == ["anthropic", "groq"]
    assert desc["primary"] == "anthropic"
    assert desc["gateway_required"] is True
    assert desc["roles"]["dev_api"] == {"tier": "heavy", "provider": "anthropic", "model": "claude-opus-4-8"}
    assert desc["roles"]["utility"]["provider"] == "anthropic"
    assert desc["roles"]["utility"]["tier"] == "light"


def test_describe_capabilities_none_connected(monkeypatch):
    _patch_live(monkeypatch, set())
    desc = cap.describe_capabilities(_S(), roles=["dev_api"])
    assert desc["connected"] == []
    assert desc["primary"] == ""
    assert desc["roles"]["dev_api"]["provider"] == ""
