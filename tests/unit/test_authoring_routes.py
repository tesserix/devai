"""HTTP-layer tests for the authoring routes."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.authoring import create_authoring_service
from devai.authoring.routes import router
from devai.specializations.registry import SpecializationRegistry


def _client() -> TestClient:
    app = FastAPI()
    app.state.authoring_service = create_authoring_service(redis=None, spec_registry=SpecializationRegistry())
    app.include_router(router)
    return TestClient(app)


def test_create_agent_via_fields_then_list():
    client = _client()
    r = client.post(
        "/api/authoring/specializations",
        json={
            "name": "ui_agent",
            "display_name": "UI Agent",
            "category": "specialist",
            "llm_provider": "anthropic",
            "system_prompt": "Be helpful.",
            "allowed_tools": ["scm_list_files"],
            "handover_schema": {"answer": {"type": "string", "required": True}},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "ui_agent"

    listed = client.get("/api/authoring/specializations").json()
    assert any(row["name"] == "ui_agent" for row in listed)


def test_create_agent_invalid_yaml_returns_422():
    client = _client()
    r = client.post("/api/authoring/specializations", json={"yaml": "description: no name\n"})
    assert r.status_code == 422


def test_create_agent_requires_name_or_yaml():
    client = _client()
    r = client.post("/api/authoring/specializations", json={})
    assert r.status_code == 422


def test_delete_unknown_agent_404():
    client = _client()
    assert client.delete("/api/authoring/specializations/nope").status_code == 404


def test_create_blueprint_roundtrip():
    client = _client()
    bp = (
        "name: ui-flow\n"
        "description: authored via UI\n"
        "stages:\n"
        "  - name: s1\n"
        "    stage: run_specialization\n"
        "    config:\n"
        "      specialization: ui_agent\n"
    )
    r = client.post("/api/authoring/blueprints", json={"yaml": bp})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "ui-flow"
    assert client.get("/api/authoring/blueprints/ui-flow").status_code == 200
