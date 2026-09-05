from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from devai.document_intelligence import routes
from devai.identity import Principal

_TEST_SIGNING_KEY = "ab" * 32
_INTERNAL_OCR_URL = "http://" + ".".join(("ocr-service", "document-intelligence", "svc", "cluster", "local")) + ":8080"
_INTERNAL_OCR_JOB_URL = (
    "http://" + ".".join(("ocr-job-service", "document-intelligence", "svc", "cluster", "local")) + ":8080"
)


@pytest.mark.asyncio
async def test_accepted_upload_creates_a_tenant_scoped_ocr_job(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        document_intelligence_service_url=_INTERNAL_OCR_URL,
        document_intelligence_job_service_url=_INTERNAL_OCR_JOB_URL,
        document_intelligence_key_id="devai-v1",
        document_intelligence_signing_key=_TEST_SIGNING_KEY,
        document_intelligence_timeout_seconds=5.0,
    )
    app.include_router(routes.router)

    async def principal(_: object) -> Principal:
        return Principal(email="user@example.test", tenant_id="tenant-a")

    class FakeClient:
        async def create_job(self, *_: object, **kwargs: object) -> dict[str, str]:
            assert kwargs == {"upload_id": "upl_01TEST", "idempotency_key": "job-1"}
            return {"job_id": "job_01TEST", "status": "accepted"}

    monkeypatch.setattr(routes, "require_principal", principal)
    monkeypatch.setattr(routes, "_client", lambda _: FakeClient())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/document-intelligence/documents/upl_01TEST/jobs",
            headers={"Idempotency-Key": "job-1"},
        )

    assert response.status_code == 202
    assert response.json() == {"job_id": "job_01TEST", "status": "accepted"}


@pytest.mark.asyncio
async def test_job_status_proxies_only_the_scoped_opaque_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        document_intelligence_service_url=_INTERNAL_OCR_URL,
        document_intelligence_job_service_url=_INTERNAL_OCR_JOB_URL,
        document_intelligence_key_id="devai-v1",
        document_intelligence_signing_key=_TEST_SIGNING_KEY,
        document_intelligence_timeout_seconds=5.0,
    )
    app.include_router(routes.router)

    async def principal(_: object) -> Principal:
        return Principal(email="user@example.test", tenant_id="tenant-a")

    class FakeClient:
        async def get_job_status(self, *_: object, **kwargs: object) -> dict[str, str]:
            assert kwargs == {"job_id": "job_01TEST"}
            return {"job_id": "job_01TEST", "status": "processing"}

    monkeypatch.setattr(routes, "require_principal", principal)
    monkeypatch.setattr(routes, "_client", lambda _: FakeClient())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/document-intelligence/jobs/job_01TEST")

    assert response.status_code == 200
    assert response.json() == {"job_id": "job_01TEST", "status": "processing"}


@pytest.mark.asyncio
async def test_job_result_proxies_only_the_bounded_sandbox_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(
        document_intelligence_service_url=_INTERNAL_OCR_URL,
        document_intelligence_job_service_url=_INTERNAL_OCR_JOB_URL,
        document_intelligence_key_id="devai-v1",
        document_intelligence_signing_key=_TEST_SIGNING_KEY,
        document_intelligence_timeout_seconds=5.0,
    )
    app.include_router(routes.router)

    async def principal(_: object) -> Principal:
        return Principal(email="user@example.test", tenant_id="tenant-a")

    class FakeClient:
        async def get_job_result(self, *_: object, **kwargs: object) -> dict[str, object]:
            assert kwargs == {"job_id": "job_01TEST"}
            return {
                "job_id": "job_01TEST",
                "summary": {"page_count": 1, "observation_count": 2, "field_count": 1, "table_count": 0, "citation_count": 1},
                "confidence": None,
                "warnings": [],
                "validation_failures": [],
                "provider": "tesserix",
                "model_version": "ocr-1",
                "processing_profile_version": "printed-en-v1",
                "duration_ms": 42,
                "cost": None,
                "fields": [],
                "text_preview": "safe literal preview",
                "text_truncated": False,
            }

    monkeypatch.setattr(routes, "require_principal", principal)
    monkeypatch.setattr(routes, "_client", lambda _: FakeClient())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/document-intelligence/jobs/job_01TEST/result")

    assert response.status_code == 200
    assert response.json()["summary"]["page_count"] == 1
    assert "object_bucket" not in response.text


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


@pytest.mark.asyncio
async def test_document_status_proxies_only_the_scoped_opaque_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
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

    class FakeClient:
        async def get_upload_status(self, *_: object, **__: object) -> dict[str, object]:
            return {"upload_id": "upl_01TEST", "status": "accepted"}

    monkeypatch.setattr(routes, "require_principal", principal)
    monkeypatch.setattr(routes, "_client", lambda _: FakeClient())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/document-intelligence/documents/upl_01TEST")

    assert response.status_code == 200
    assert response.json() == {"upload_id": "upl_01TEST", "status": "accepted"}
    assert "storage" not in response.text
