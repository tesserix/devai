from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from devai.document_intelligence.client import DocumentIntelligenceClient
from devai.identity import Principal

_TEST_SIGNING_KEY = "ab" * 32
_INTERNAL_OCR_URL = "http://" + ".".join(("ocr-service", "document-intelligence", "svc", "cluster", "local")) + ":8080"
_INTERNAL_OCR_JOB_URL = (
    "http://" + ".".join(("ocr-job-service", "document-intelligence", "svc", "cluster", "local")) + ":8080"
)


@pytest.mark.asyncio
async def test_create_job_uses_the_dedicated_job_capability_endpoint() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["headers"] = dict(request.headers)
        observed["body"] = request.content
        return httpx.Response(
            202,
            json={
                "job_id": "job_01TEST",
                "status": "accepted",
                "created_at": "2026-09-05T00:00:00Z",
                "status_url": "/v1/ocr/jobs/job_01TEST",
                "result_url": "/v1/ocr/jobs/job_01TEST/result",
            },
        )

    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await client.create_job(
            http,
            Principal(email="user@example.test", tenant_id="tenant-a"),
            upload_id="upl_01TEST",
            idempotency_key="job-1",
        )

    assert response == {"job_id": "job_01TEST", "status": "accepted"}
    assert observed["url"] == f"{_INTERNAL_OCR_JOB_URL}/v1/ocr/jobs"
    assert observed["headers"]["idempotency-key"] == "job-1"
    assert observed["headers"]["x-ocr-signature"]
    assert b"upl_01TEST" in observed["body"]


@pytest.mark.asyncio
async def test_job_status_uses_the_dedicated_job_capability_endpoint() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "job_id": "job_01TEST",
                "status": "completed",
                "created_at": "2026-09-05T00:00:00Z",
            },
        )

    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await client.get_job_status(
            http,
            Principal(email="user@example.test", tenant_id="tenant-a"),
            job_id="job_01TEST",
        )

    assert response == {"job_id": "job_01TEST", "status": "completed"}
    assert observed["url"] == f"{_INTERNAL_OCR_JOB_URL}/v1/ocr/jobs/job_01TEST"
    assert observed["headers"]["x-ocr-signature"]


@pytest.mark.asyncio
async def test_upload_intent_uses_a_server_signed_tenant_scope() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["headers"] = dict(request.headers)
        observed["body"] = request.content
        return httpx.Response(
            201,
            json={
                "upload_id": "upl_01TEST",
                "method": "PUT",
                "upload_url": "https://storage.example.test/upload",
                "required_headers": {"content-type": "image/png"},
                "expires_at": "2026-09-04T05:00:00Z",
            },
        )

    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    principal = Principal(email="user@example.test", tenant_id="tenant-a")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await client.create_upload_intent(
            http,
            principal,
            content_type="image/png",
            content_length=1024,
            sha256="sha256:" + "a" * 64,
            idempotency_key="intent-1",
        )

    expected_tenant = "ten_devai_" + hashlib.sha256(b"tenant-a").hexdigest()[:32]
    expected_message = (
        f"devai-v1\n{expected_tenant}\n{observed['headers']['x-ocr-timestamp']}\nPOST\n/v1/ocr/uploads"
    ).encode()
    expected_signature = hmac.new(
        bytes.fromhex(_TEST_SIGNING_KEY),
        expected_message,
        hashlib.sha256,
    ).hexdigest()

    assert response["upload_id"] == "upl_01TEST"
    assert observed["url"] == f"{_INTERNAL_OCR_URL}/v1/ocr/uploads"
    assert observed["headers"]["x-ocr-tenant-id"] == expected_tenant
    assert observed["headers"]["x-ocr-signature"] == expected_signature
    assert "tenant-a" not in observed["body"].decode()


@pytest.mark.asyncio
async def test_upload_intent_rejects_an_unscoped_principal_without_network_io() -> None:
    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    principal = Principal(email="user@example.test")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network call was not expected"))
    ) as http:
        with pytest.raises(ValueError, match="tenant"):
            await client.create_upload_intent(
                http,
                principal,
                content_type="image/png",
                content_length=1024,
                sha256="sha256:" + "a" * 64,
                idempotency_key="intent-1",
            )


@pytest.mark.asyncio
async def test_upload_completion_signs_the_opaque_upload_identifier() -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["signature"] = request.headers["x-ocr-signature"]
        return httpx.Response(200, json={"upload_id": "upl_01TEST", "status": "uploaded"})

    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await client.complete_upload(
            http,
            Principal(email="user@example.test", tenant_id="tenant-a"),
            upload_id="upl_01TEST",
        )

    assert response["status"] == "uploaded"
    assert observed["path"] == "/v1/ocr/uploads/upl_01TEST/complete"
    assert observed["signature"]


@pytest.mark.asyncio
async def test_upload_status_signs_and_returns_only_the_opaque_lifecycle_state() -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["signature"] = request.headers["x-ocr-signature"]
        return httpx.Response(200, json={"upload_id": "upl_01TEST", "status": "inspecting"})

    client = DocumentIntelligenceClient(
        base_url=_INTERNAL_OCR_URL,
        job_base_url=_INTERNAL_OCR_JOB_URL,
        key_id="devai-v1",
        signing_key=_TEST_SIGNING_KEY,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await client.get_upload_status(
            http,
            Principal(email="user@example.test", tenant_id="tenant-a"),
            upload_id="upl_01TEST",
        )

    assert response == {"upload_id": "upl_01TEST", "status": "inspecting"}
    assert observed["path"] == "/v1/ocr/uploads/upl_01TEST"
    assert observed["signature"]
