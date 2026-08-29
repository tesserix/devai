"""Agent Cards this platform publishes about its own specializations.

``card.py`` is the consumer side — it parses cards other agents serve. This is
the producer side, so downstream products can discover what DevAI exposes over
A2A instead of being handed a hardcoded list of agent names.
"""

from __future__ import annotations

from typing import Any

from devai import __version__
from devai.specializations.base import Specialization

PROTOCOL_VERSION = "0.3.0"

_INPUT_MODES = ["text/plain"]
_OUTPUT_MODES = ["application/json"]
_PROVIDER = {"organization": "tesserix", "url": "https://tesserix.app"}


def agent_id(spec: Specialization) -> str:
    """Registry capability name (``requirements_analyst``) as its A2A skill id."""
    return spec.name.replace("_", "-")


def _skill(spec: Specialization) -> dict[str, Any]:
    tags = [spec.category] if spec.category else []
    # A critical-risk agent answers 409 on message/send until a human approves,
    # so say that on the card rather than let consumers discover it by failing.
    if spec.risk_level.needs_human_gate:
        tags.append("requires-approval")
    return {
        "id": agent_id(spec),
        "name": spec.display_name or spec.name.replace("_", " ").title(),
        "description": spec.description,
        "tags": tags,
        "inputModes": _INPUT_MODES,
        "outputModes": _OUTPUT_MODES,
    }


def _card(*, name: str, description: str, url: str, skills: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "version": __version__,
        "url": url,
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": _INPUT_MODES,
        "defaultOutputModes": _OUTPUT_MODES,
        "provider": _PROVIDER,
        "skills": skills,
    }


def build_platform_card(specs: list[Specialization], base_url: str) -> dict[str, Any]:
    """One card for the whole catalog — each admitted agent is a skill."""
    root = base_url.rstrip("/")
    return _card(
        name="DevAI",
        description="DevAI agent catalog: registry-composed agents callable over A2A.",
        url=f"{root}/a2a/v1",
        skills=[_skill(spec) for spec in sorted(specs, key=lambda s: s.name)],
    )


def build_agent_card(spec: Specialization, base_url: str) -> dict[str, Any]:
    """Card for a single agent, addressed at its own send endpoint."""
    root = base_url.rstrip("/")
    return _card(
        name=spec.display_name or spec.name.replace("_", " ").title(),
        description=spec.description,
        url=f"{root}/a2a/v1/{agent_id(spec)}",
        skills=[_skill(spec)],
    )


__all__ = ["PROTOCOL_VERSION", "build_agent_card", "build_platform_card"]
