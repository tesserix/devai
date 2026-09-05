"""Server-side client for the shared document-intelligence API."""

from __future__ import annotations

import hashlib
import hmac
import json
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
_JOB_ID_PATTERN = re.compile(r"^job_[A-Za-z0-9_]{1,64}$")
_UPLOAD_STATUSES = frozenset({"reserved", "uploaded", "inspecting", "accepted", "rejected", "expired"})
_JOB_STATUSES = frozenset(
    {
        "accepted",
        "inspecting",
        "processing",
        "validating",
        "cancelling",
        "cancelled",
        "rejected",
        "partial",
        "review_required",
        "completed",
    }
)
_MAXIMUM_RESULT_TEXT_PREVIEW_CHARS = 10_000
_MAXIMUM_RESULT_FIELDS = 100
_MAXIMUM_RESULT_VALUE_BYTES = 32 * 1024
_CONFIDENCE_DIMENSIONS = frozenset({"input_quality", "ocr", "classification", "extraction", "validation", "overall"})
_STABLE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DocumentIntelligenceError(ValueError):
    """Raised when a document-intelligence request cannot be safely sent."""


class DocumentIntelligenceClient:
    """Signs product-scoped requests after DevAI has verified the caller."""

    def __init__(self, *, base_url: str, job_base_url: str, key_id: str, signing_key: str) -> None:
        self._upload_base_url = _validate_base_url(base_url)
        self._job_base_url = _validate_base_url(job_base_url)
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
            "DocumentIntelligenceClient("
            f"upload_base_url={self._upload_base_url!r}, job_base_url={self._job_base_url!r}, "
            f"key_id={self._key_id!r}, signing_key=[redacted])"
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
            f"{self._upload_base_url}{path}",
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
            f"{self._upload_base_url}{path}",
            headers=self._signed_headers(principal, "POST", path),
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return payload

    async def get_upload_status(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        upload_id: str,
    ) -> dict[str, str]:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise DocumentIntelligenceError("document upload identifier is invalid")
        path = f"/v1/ocr/uploads/{upload_id}"
        response = await http.get(
            f"{self._upload_base_url}{path}",
            headers=self._signed_headers(principal, "GET", path),
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"upload_id", "status"}
            or payload.get("upload_id") != upload_id
            or payload.get("status") not in _UPLOAD_STATUSES
        ):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return {"upload_id": upload_id, "status": str(payload["status"])}

    async def create_job(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        upload_id: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if not _UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise DocumentIntelligenceError("document upload identifier is invalid")
        _validate_idempotency_key(idempotency_key)
        path = "/v1/ocr/jobs"
        response = await http.post(
            f"{self._job_base_url}{path}",
            headers={
                **self._signed_headers(principal, "POST", path),
                "Idempotency-Key": idempotency_key,
            },
            json={
                "source": {"upload_id": upload_id},
                "document_type": "auto",
                "processing_class": "interactive",
            },
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"job_id", "status", "created_at", "status_url", "result_url"}
            or not isinstance(payload.get("job_id"), str)
            or not _JOB_ID_PATTERN.fullmatch(str(payload["job_id"]))
            or payload.get("status") not in _JOB_STATUSES
            or not isinstance(payload.get("created_at"), str)
            or not isinstance(payload.get("status_url"), str)
            or not isinstance(payload.get("result_url"), str)
        ):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return {"job_id": str(payload["job_id"]), "status": str(payload["status"])}

    async def get_job_status(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        job_id: str,
    ) -> dict[str, str]:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise DocumentIntelligenceError("document job identifier is invalid")
        path = f"/v1/ocr/jobs/{job_id}"
        response = await http.get(
            f"{self._job_base_url}{path}",
            headers=self._signed_headers(principal, "GET", path),
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"job_id", "status", "created_at"}
            or payload.get("job_id") != job_id
            or payload.get("status") not in _JOB_STATUSES
            or not isinstance(payload.get("created_at"), str)
        ):
            raise DocumentIntelligenceError("document-intelligence response is invalid")
        return {"job_id": job_id, "status": str(payload["status"])}

    async def get_job_result(
        self,
        http: httpx.AsyncClient,
        principal: Principal,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise DocumentIntelligenceError("document job identifier is invalid")
        path = f"/v1/ocr/jobs/{job_id}/result"
        response = await http.get(
            f"{self._job_base_url}{path}",
            headers=self._signed_headers(principal, "GET", path),
        )
        if response.is_error:
            raise DocumentIntelligenceError(f"document-intelligence request failed with status {response.status_code}")
        return _sandbox_result_view(response.json(), job_id)

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


def _sandbox_result_view(payload: object, job_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0" or payload.get("content_trust") != "untrusted":
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    text = payload.get("text")
    pages = payload.get("pages")
    fields = payload.get("fields")
    tables = payload.get("tables")
    citations = payload.get("citations")
    if not isinstance(text, str) or not isinstance(pages, list) or not isinstance(fields, dict) or not isinstance(tables, list) or not isinstance(citations, list):
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    if len(pages) > 300:
        raise DocumentIntelligenceError("document-intelligence result is invalid")

    observation_count = 0
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("observations"), list):
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        observation_count += len(page["observations"])

    confidence = _confidence_dimensions(payload.get("confidence"))
    warnings = _stable_codes(payload.get("warnings"))
    validation_failures = _validation_failures(payload.get("validation_failures"))
    result_fields = _result_fields(fields)
    provider = _optional_version_name(payload.get("provider"))
    model_version = _optional_version_name(payload.get("model_version"))
    profile = _optional_version_name(payload.get("processing_profile_version"))
    duration_ms = payload.get("duration_ms")
    cost = _cost(payload.get("cost"))
    if not (duration_ms is None or isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0):
        raise DocumentIntelligenceError("document-intelligence result is invalid")

    return {
        "job_id": job_id,
        "summary": {
            "page_count": len(pages),
            "observation_count": observation_count,
            "field_count": len(fields),
            "table_count": len(tables),
            "citation_count": len(citations),
        },
        "confidence": confidence,
        "warnings": warnings,
        "validation_failures": validation_failures,
        "provider": provider,
        "model_version": model_version,
        "processing_profile_version": profile,
        "duration_ms": duration_ms,
        "cost": cost,
        "fields": result_fields,
        "text_preview": text[:_MAXIMUM_RESULT_TEXT_PREVIEW_CHARS],
        "text_truncated": len(text) > _MAXIMUM_RESULT_TEXT_PREVIEW_CHARS,
    }


def _confidence_dimensions(value: object) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _CONFIDENCE_DIMENSIONS:
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    dimensions: dict[str, float] = {}
    for name in _CONFIDENCE_DIMENSIONS:
        score = value[name]
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        dimensions[name] = float(score)
    return dimensions


def _stable_codes(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    if not all(isinstance(code, str) and _STABLE_CODE_PATTERN.fullmatch(code) for code in value):
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    return list(value)


def _validation_failures(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 100:
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    failures: list[dict[str, str]] = []
    for failure in value:
        if (
            not isinstance(failure, dict)
            or set(failure) != {"code", "severity"}
            or not isinstance(failure["code"], str)
            or not _STABLE_CODE_PATTERN.fullmatch(failure["code"])
            or failure["severity"] not in {"warning", "error"}
        ):
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        failures.append({"code": failure["code"], "severity": failure["severity"]})
    return failures


def _result_fields(value: dict[object, object]) -> list[dict[str, Any]]:
    if len(value) > _MAXIMUM_RESULT_FIELDS:
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    fields: list[dict[str, Any]] = []
    for name, field in sorted(value.items()):
        if not isinstance(name, str) or not (1 <= len(name) <= 128) or not isinstance(field, dict):
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        score = field.get("confidence")
        evidence = field.get("evidence")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1 or not isinstance(evidence, list):
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        pages = sorted({item.get("page") for item in evidence if isinstance(item, dict) and isinstance(item.get("page"), int)})
        if not pages or any(page < 1 or page > 300 for page in pages):
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        field_value = field.get("value")
        try:
            encoded = json.dumps(field_value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise DocumentIntelligenceError("document-intelligence result is invalid") from error
        if len(encoded.encode()) > _MAXIMUM_RESULT_VALUE_BYTES:
            raise DocumentIntelligenceError("document-intelligence result is invalid")
        fields.append({"name": name, "value": field_value, "confidence": float(score), "pages": pages})
    return fields


def _optional_version_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _VERSION_NAME_PATTERN.fullmatch(value):
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    return value


def _cost(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"currency", "decimal"}
        or not isinstance(value["currency"], str)
        or not re.fullmatch(r"[A-Z]{3}", value["currency"])
        or not isinstance(value["decimal"], str)
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value["decimal"])
    ):
        raise DocumentIntelligenceError("document-intelligence result is invalid")
    return {"currency": value["currency"], "decimal": value["decimal"]}


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
    _validate_idempotency_key(idempotency_key)


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not 1 <= len(idempotency_key) <= 128 or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise DocumentIntelligenceError("document idempotency key is invalid")
