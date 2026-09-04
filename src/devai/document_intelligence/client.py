"""Server-side client for the shared document-intelligence API."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from devai.identity import Principal

if TYPE_CHECKING:
    import httpx

_MAXIMUM_UPLOAD_BYTES = 100 * 1024 * 1024
_SUPPORTED_CONTENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png", "image/tiff", "image/webp"})
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_UPLOAD_ID_PATTERN = re.compile(r"^upl_[A-Za-z0-9_]{1,64}$")


class DocumentIntelligenceError(ValueError):
    """Raised when a document-intelligence request cannot be safely sent."""


class DocumentIntelligenceClient:
    """Signs product-scoped requests after DevAI has verified the caller."""

    def __init__(self, *, base_url: str, key_id: str, signing_key: str) -> None:
        self._base_url = _validate_base_url(base_url)
        if not _KEY_ID_PATTERN.fullmatch(key_id):
            raise DocumentIntelligenceError("document-intelligence key identifier is invalid")
        try:
            key = bytes.fromhex(signing_key)
        except ValueError as error:
            raise DocumentIntelligenceError("document-intelligence signing key is invalid") from error
        if len(key) < 32:
            raise DocumentIntelligenceError("document-intelligence signing key is too short")
        self._key_id = key_id
        self._signing_key = key

    def __repr__(self) -> str:
        return (
            f"DocumentIntelligenceClient(base_url={self._base_url!r}, key_id={self._key_id!r}, signing_key=[redacted])"
        )

    async def create_upload_intent(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        content_type: str,
        content_length: int,
        sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _validate_upload_request(content_type, content_length, sha256, idempotency_key)
        path = "/v1/ocr/uploads"
        response = await http.post(
            f"{self._base_url}{path}",
            headers={
                **self._signed_headers(principal, "POST", path),
                "Idempotency-Key": idempotency_key,
            },
            json={
                "content_type": content_type,
                "content_length": content_length,
                "sha256": sha256,
            },
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return payload

    async def complete_upload(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        upload_id: str,
    ) -> dict[str, Any]:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise DocumentIntelligenceError("document upload identifier is invalid")
        path = f"/v1/ocr/uploads/{upload_id}/complete"
        response = await http.post(
            f"{self._base_url}{path}",
            headers=self._signed_headers(principal, "POST", path),
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return payload

    def _signed_headers(self, principal: Principal, method: str, path_and_query: str) -> dict[str, str]:
        tenant_id = _ocr_tenant_id(principal)
        timestamp = str(int(time.time()))
        message = f"{self._key_id}\n{tenant_id}\n{timestamp}\n{method}\n{path_and_query}".encode()
        signature = hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
        return {
            "X-OCR-Key-Id": self._key_id,
            "X-OCR-Tenant-Id": tenant_id,
            "X-OCR-Timestamp": timestamp,
            "X-OCR-Signature": signature,
        }


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or not parsed.hostname.endswith(".svc.cluster.local")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise DocumentIntelligenceError("document-intelligence URL must be an internal service URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _ocr_tenant_id(principal: Principal) -> str:
    tenant_id = principal.tenant_id.strip()
    if not tenant_id:
        raise DocumentIntelligenceError("document-intelligence requires a tenant-scoped principal")
    digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:32]
    return f"ten_devai_{digest}"


def _validate_upload_request(content_type: str, content_length: int, sha256: str, idempotency_key: str) -> None:
    if content_type not in _SUPPORTED_CONTENT_TYPES:
        raise DocumentIntelligenceError("document content type is unsupported")
    if not 0 < content_length <= _MAXIMUM_UPLOAD_BYTES:
        raise DocumentIntelligenceError("document content length is invalid")
    if not _DIGEST_PATTERN.fullmatch(sha256):
        raise DocumentIntelligenceError("document digest is invalid")
    if not 1 <= len(idempotency_key) <= 128 or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise DocumentIntelligenceError("document idempotency key is invalid")
