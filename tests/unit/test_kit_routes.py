"""HTTP layer for the runtime version picker — /api/adk/versions."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.kit.routes import router
from devai.kit.versions import AdkVersionCatalogue


def _catalogue(tags: list[str] | None = None) -> AdkVersionCatalogue:
    async def fetch() -> list[dict]:
        return [{"tag_name": t, "draft": False, "prerelease": False} for t in (tags or ["v0.1.1", "v0.1.0"])]

    return AdkVersionCatalogue(fetch=fetch, fallback="0.1.1")


def _client(*, wired: bool = True) -> TestClient:
    app = FastAPI()
    app.state.adk_catalogue = _catalogue() if wired else None
    app.include_router(router)
    return TestClient(app)


def test_offers_the_versions_with_the_latest_as_the_default() -> None:
    body = _client().get("/api/adk/versions").json()

    assert body["versions"] == ["0.1.1", "0.1.0"]
    assert body["default"] == "0.1.1"


def test_never_offers_more_than_five() -> None:
    app = FastAPI()
    app.state.adk_catalogue = _catalogue([f"v0.1.{n}" for n in range(9, 0, -1)])
    app.include_router(router)

    assert len(TestClient(app).get("/api/adk/versions").json()["versions"]) == 5


def test_503s_until_the_catalogue_is_wired() -> None:
    assert _client(wired=False).get("/api/adk/versions").status_code == 503
