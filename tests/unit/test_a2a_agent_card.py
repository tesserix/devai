"""A2A discovery: the cards this platform publishes about its own agents."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.a2a.routes import router, well_known_router
from devai.config import Settings
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.service import SpecializationService

_AUTH = {
    "X-Forwarded-User": "owner@example.com",
    "X-Forwarded-Uid": "owner-1",
    "X-Forwarded-Tenant": "tenant-a",
    "X-Auth-Bff-Secret": "test-shared-secret",
}


def _client() -> TestClient:
    config = Settings(
        auth_bff_shared_secret="test-shared-secret",
        public_base_url="https://devai.example.com",
    )
    registry = SpecializationRegistry()
    registry.register(
        load_specialization_from_string(
            """
name: requirements_analyst
display_name: Requirements Analyst
description: Turns a request into testable requirements.
category: planning
runtime: tesserix_adk
system_prompt: Analyze requirements.
handover_schema:
  summary: string
"""
        )
    )
    registry.register(
        load_specialization_from_string(
            """
name: release_manager
description: Ships approved changes.
risk_level: critical
runtime: tesserix_adk
system_prompt: Release approved changes.
handover_schema:
  summary: string
"""
        )
    )
    service = SpecializationService(config)
    service._registry = registry
    service._reviewed_capabilities = frozenset(spec.name for spec in registry.all())
    service._started = True
    app = FastAPI()
    app.state.config = config
    app.state.specialization_service = service
    app.include_router(router)
    app.include_router(well_known_router)
    return TestClient(app)


def test_platform_card_is_served_at_the_well_known_path() -> None:
    response = _client().get("/.well-known/agent-card.json", headers=_AUTH)

    assert response.status_code == 200
    card = response.json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"] == "https://devai.example.com/a2a/v1"
    assert card["preferredTransport"] == "JSONRPC"


def test_platform_card_advertises_every_admitted_agent_as_a_skill() -> None:
    card = _client().get("/.well-known/agent-card.json", headers=_AUTH).json()

    skills = {skill["id"]: skill for skill in card["skills"]}
    assert set(skills) == {"requirements-analyst", "release-manager"}
    assert skills["requirements-analyst"]["name"] == "Requirements Analyst"
    assert skills["requirements-analyst"]["description"] == "Turns a request into testable requirements."
    assert "planning" in skills["requirements-analyst"]["tags"]


def test_gated_agents_are_tagged_so_consumers_do_not_call_them_blind() -> None:
    """A critical-risk agent answers 409, not 200, so the card must say so."""
    card = _client().get("/.well-known/agent-card.json", headers=_AUTH).json()

    skills = {skill["id"]: skill for skill in card["skills"]}
    assert "requires-approval" in skills["release-manager"]["tags"]
    assert "requires-approval" not in skills["requirements-analyst"]["tags"]


def test_per_agent_card_points_at_that_agents_send_endpoint() -> None:
    response = _client().get("/a2a/v1/requirements-analyst/card", headers=_AUTH)

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Requirements Analyst"
    assert card["url"] == "https://devai.example.com/a2a/v1/requirements-analyst"
    assert [skill["id"] for skill in card["skills"]] == ["requirements-analyst"]


def test_per_agent_card_accepts_the_agent_suffix_like_the_send_route() -> None:
    response = _client().get("/a2a/v1/requirements-analyst-agent/card", headers=_AUTH)

    assert response.status_code == 200
    assert response.json()["url"] == "https://devai.example.com/a2a/v1/requirements-analyst"


def test_unknown_agent_card_is_404() -> None:
    assert _client().get("/a2a/v1/nope/card", headers=_AUTH).status_code == 404


def test_cards_require_a_verified_principal() -> None:
    client = _client()

    assert client.get("/.well-known/agent-card.json").status_code == 401
    assert client.get("/a2a/v1/requirements-analyst/card").status_code == 401
