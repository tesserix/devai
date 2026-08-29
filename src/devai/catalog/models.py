"""The models DevAI offers, grouped by the provider that would serve them.

A model id alone is not a choice: the same Claude model is one thing on an
Anthropic key and another through the gateway or Vertex, with different
credentials, quota and billing behind it. So the catalog is keyed by provider,
and a model that two providers can serve appears under both.

Tiers are not stored — :func:`tier_for_model` derives them, so this file cannot
disagree with the router about what a model is. Everything the router would
pick (``PROVIDER_TIER_MODELS``) is offerable here; the contract test enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devai.adapters.llm.capabilities import tier_for_model

DEFAULT_PROVIDER = "anthropic"

# The Claude ids, shared by every provider that can route them.
_CLAUDE = (
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
)
_GEMINI = (
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
)
_OPENAI = (
    ("o3", "OpenAI o3"),
    ("gpt-4.1", "GPT-4.1"),
    ("gpt-4.1-mini", "GPT-4.1 mini"),
)


@dataclass(frozen=True)
class Model:
    id: str
    label: str

    @property
    def tier(self) -> str:
        return tier_for_model(self.id)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "tier": self.tier}


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    models: tuple[Model, ...]
    default_model: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "default_model": self.default_model,
            "models": [m.to_dict() for m in self.models],
        }


def _models(*pairs: tuple[str, str]) -> tuple[Model, ...]:
    return tuple(Model(id=i, label=label) for i, label in pairs)


CATALOG: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            id="anthropic",
            label="Anthropic",
            models=_models(*_CLAUDE),
            default_model="claude-sonnet-4-6",
            description="Claude, direct on an Anthropic API key.",
        ),
        Provider(
            id="openai",
            label="OpenAI",
            models=_models(*_OPENAI),
            default_model="gpt-4.1",
            description="GPT and o-series, direct on an OpenAI key.",
        ),
        Provider(
            id="vertex_gemini",
            label="Google Vertex AI",
            models=_models(*_GEMINI),
            default_model="gemini-2.5-pro",
            description="Gemini on your own GCP project — keyless on GKE.",
        ),
        Provider(
            id="gateway",
            label="LLM Gateway",
            models=_models(*_CLAUDE, *_GEMINI, *_OPENAI),
            default_model="claude-sonnet-4-6",
            description="One endpoint that routes any alias to any backend, including Claude on Vertex.",
        ),
        Provider(
            id="groq",
            label="Groq",
            models=_models(
                ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
                ("llama-3.1-8b-instant", "Llama 3.1 8B"),
            ),
            default_model="llama-3.3-70b-versatile",
            description="Open models at very low latency.",
        ),
        Provider(
            id="openrouter",
            label="OpenRouter",
            models=_models(
                ("anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5"),
                ("google/gemini-2.5-pro", "Gemini 2.5 Pro"),
                ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B"),
                ("deepseek/deepseek-chat", "DeepSeek Chat"),
            ),
            default_model="anthropic/claude-sonnet-4.5",
            description="One key, hundreds of models.",
        ),
    )
}


def provider_ids() -> tuple[str, ...]:
    return tuple(CATALOG)


def models_for(provider: str) -> tuple[Model, ...]:
    entry = CATALOG.get((provider or "").strip().lower())
    return entry.models if entry else ()


def default_model_for(provider: str) -> str:
    entry = CATALOG.get((provider or "").strip().lower())
    return entry.default_model if entry else ""


def default_provider() -> str:
    return DEFAULT_PROVIDER


__all__ = [
    "CATALOG",
    "Model",
    "Provider",
    "default_model_for",
    "default_provider",
    "models_for",
    "provider_ids",
]
