"""Authenticated DevAI test-console routes for document intelligence."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from tempfile import SpooledTemporaryFile
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile

from devai.authz import require_principal
from devai.document_intelligence.client import DocumentIntelligenceClient, DocumentIntelligenceError

router = APIRouter(prefix="/api/document-intelligence", tags=["document-intelligence"])
_MAXIMUM_UPLOAD_BYTES = 100 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = await require_principal(request)
    content_type = file.content_type or ""
    try:
        with SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as staged:
            content_length, sha256 = await _stage_document(file, staged)
            client = _client(request)
            async with httpx.AsyncClient(
                timeout=request.app.state.config.document_intelligence_timeout_seconds
            ) as http:
                intent = await client.create_upload_intent(
                    http,
                    principal,
                    content_type=content_type,
                    content_length=content_length,
                    sha256=sha256,
                    idempotency_key=idempotency_key,
                )
                await _upload_staged_document(http, intent, staged, content_length)
                return await client.complete_upload(http, principal, upload_id=str(intent["upload_id"]))
    except UploadTooLargeError as error:
        raise HTTPException(status_code=413, detail="document exceeds the upload limit") from error
    except DocumentIntelligenceError as error:
        raise HTTPException(status_code=422, detail="document request was rejected") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail="document service is unavailable") from error
    finally:
        await file.close()


@router.get("/documents/{upload_id}")
async def get_document_status(request: Request, upload_id: str) -> dict[str, str]:
    principal = await require_principal(request)
    try:
        client = _client(request)
        async with httpx.AsyncClient(timeout=request.app.state.config.document_intelligence_timeout_seconds) as http:
            return await client.get_upload_status(http, principal, upload_id=upload_id)
    except DocumentIntelligenceError as error:
        raise HTTPException(status_code=422, detail="document status was rejected") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail="document service is unavailable") from error


class UploadTooLargeError(ValueError):
    """Raised when a sandbox upload exceeds the service's hard size limit."""


class StagedDocument(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def write(self, data: bytes) -> int: ...


async def _stage_document(file: UploadFile, staged: StagedDocument) -> tuple[int, str]:
    digest = hashlib.sha256()
    content_length = 0
    while chunk := await file.read(_CHUNK_BYTES):
        content_length += len(chunk)
        if content_length > _MAXIMUM_UPLOAD_BYTES:
            raise UploadTooLargeError
        digest.update(chunk)
        await asyncio.to_thread(staged.write, chunk)
    await asyncio.to_thread(staged.seek, 0)
    return content_length, f"sha256:{digest.hexdigest()}"


async def _upload_staged_document(
    http: httpx.AsyncClient,
    intent: dict[str, Any],
    staged: StagedDocument,
    content_length: int,
) -> None:
    upload_url = intent.get("upload_url")
    method = intent.get("method")
    required_headers = intent.get("required_headers")
    if not isinstance(upload_url, str) or not isinstance(method, str) or not isinstance(required_headers, dict):
        raise DocumentIntelligenceError("document-intelligence upload intent is invalid")
    headers = {str(name): str(value) for name, value in required_headers.items()}
    headers["Content-Length"] = str(content_length)
    response = await http.request(method, upload_url, headers=headers, content=_staged_chunks(staged))
    if response.is_error:
        raise DocumentIntelligenceError(f"document storage upload failed with status {response.status_code}")


async def _staged_chunks(staged: StagedDocument) -> AsyncIterator[bytes]:
    while chunk := await asyncio.to_thread(staged.read, _CHUNK_BYTES):
        yield chunk


def _client(request: Request) -> DocumentIntelligenceClient:
    config = request.app.state.config
    signing_key = config.document_intelligence_signing_key
    if hasattr(signing_key, "get_secret_value"):
        signing_key = signing_key.get_secret_value()
    if not config.document_intelligence_service_url or not config.document_intelligence_key_id or not signing_key:
        raise HTTPException(status_code=503, detail="document service is not configured")
    try:
        return DocumentIntelligenceClient(
            base_url=config.document_intelligence_service_url,
            key_id=config.document_intelligence_key_id,
            signing_key=str(signing_key),
        )
    except DocumentIntelligenceError as error:
        raise HTTPException(status_code=503, detail="document service is not configured") from error
