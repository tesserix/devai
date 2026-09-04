from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from devai.document_intelligence import routes
from devai.identity import Principal

_TEST_SIGNING_KEY = "ab" * 32
_INTERNAL_OCR_URL = "http://" + ".".join(("ocr-service", "document-intelligence", "svc", "cluster", "local")) + ":8080"


@pytest.mark.asyncio
async def test_upload_intent_requires_a_verified_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        document_intelligence_service_url=_INTERNAL_OCR_URL,
        document_intelligence_key_id="devai-v1",
        document_intelligence_signing_key=_TEST_SIGNING_KEY,
        document_intelligence_timeout_seconds=5.0,
    )
    app.include_router(routes.router)

    async def missing_principal(_: object) -> Principal:
        raise routes.HTTPException(status_code=401, detail="authentication required")

    monkeypatch.setattr(routes, "require_principal", missing_principal)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/document-intelligence/documents",
            files={"file": ("scan.png", b"image-bytes", "image/png")},
            headers={"Idempotency-Key": "intent-1"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_document_upload_keeps_signed_storage_capability_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        document_intelligence_service_url=_INTERNAL_OCR_URL,
        document_intelligence_key_id="devai-v1",
        document_intelligence_signing_key=_TEST_SIGNING_KEY,
        document_intelligence_timeout_seconds=5.0,
    )
    app.include_router(routes.router)

    async def principal(_: object) -> Principal:
        return Principal(email="user@example.test", tenant_id="tenant-a")

    async def upload(*_: object) -> None:
        return None

    class FakeClient:
        async def create_upload_intent(self, *_: object, **__: object) -> dict[str, object]:
            return {
                "upload_id": "upl_01TEST",
                "method": "PUT",
                "upload_url": "https://storage.example.test/upload",
                "required_headers": {"content-type": "image/png"},
            }

        async def complete_upload(self, *_: object, **__: object) -> dict[str, object]:
            return {"upload_id": "upl_01TEST", "status": "uploaded"}

    monkeypatch.setattr(routes, "require_principal", principal)
    monkeypatch.setattr(routes, "_client", lambda _: FakeClient())
    monkeypatch.setattr(routes, "_upload_staged_document", upload)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/document-intelligence/documents",
            files={"file": ("scan.png", b"image-bytes", "image/png")},
            headers={"Idempotency-Key": "intent-1"},
        )

    assert response.status_code == 200
    assert response.json() == {"upload_id": "upl_01TEST", "status": "uploaded"}
    assert "storage.example.test" not in response.text
