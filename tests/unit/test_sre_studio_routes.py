from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import devai.sre_studio.routes as sre_studio_routes
from devai.identity import Principal


class _Service:
    async def list_drafts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return []

    async def get_draft(self, draft_id: str) -> dict[str, Any]:
        return {"id": draft_id}

    async def create_draft(
        self,
        kind: str,
        yaml_text: str,
        *,
        created_by: str,
        description: str,
    ) -> dict[str, Any]:
        return {"kind": kind, "yaml": yaml_text, "created_by": created_by, "description": description}

    async def update_draft(
        self,
        draft_id: str,
        *,
        yaml_text: str | None,
        name: str | None,
        description: str | None,
    ) -> dict[str, Any]:
        return {"id": draft_id, "yaml": yaml_text, "name": name, "description": description}

    async def delete_draft(self, draft_id: str) -> bool:
        return True

    async def dry_run(self, draft_id: str, *, cluster_id: str) -> dict[str, Any]:
        return {"id": draft_id, "cluster_id": cluster_id}

    async def publish(self, draft_id: str, *, created_by: str) -> dict[str, Any]:
        return {"id": draft_id, "created_by": created_by}


def _client(monkeypatch: pytest.MonkeyPatch, principal: Principal | None = None) -> TestClient:
    async def _extract_principal(_request: Request) -> Principal | None:
        return principal

    monkeypatch.setattr(sre_studio_routes, "extract_principal", _extract_principal)
    app = FastAPI()
    app.include_router(sre_studio_routes.router)
    app.state.sre_studio_service = _Service()
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/api/sre-studio/drafts", None, id="list"),
        pytest.param("GET", "/api/sre-studio/drafts/draft-1", None, id="get"),
        pytest.param(
            "POST",
            "/api/sre-studio/drafts",
            {"kind": "blueprint", "yaml": "name: example"},
            id="create",
        ),
        pytest.param(
            "PATCH",
            "/api/sre-studio/drafts/draft-1",
            {"description": "updated"},
            id="update",
        ),
        pytest.param("DELETE", "/api/sre-studio/drafts/draft-1", None, id="delete"),
        pytest.param(
            "POST",
            "/api/sre-studio/drafts/draft-1/dry-run",
            {"cluster_id": "default"},
            id="dry-run",
        ),
        pytest.param("POST", "/api/sre-studio/drafts/draft-1/publish", None, id="publish"),
    ],
)
def test_sre_studio_routes_require_authentication(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    response = _client(monkeypatch).request(method, path, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_create_and_publish_use_the_authenticated_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, Principal(email="alice@example.com", uid="alice"))

    created = client.post(
        "/api/sre-studio/drafts",
        json={"kind": "blueprint", "yaml": "name: example"},
    )
    published = client.post("/api/sre-studio/drafts/draft-1/publish")

    assert created.status_code == 201
    assert created.json()["created_by"] == "alice@example.com"
    assert published.status_code == 200
    assert published.json()["created_by"] == "alice@example.com"
