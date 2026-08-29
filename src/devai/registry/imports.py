"""Immutable Registry agent imports for reproducible sandbox evaluation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.registry.client import ResolvedAgent


_REGISTRY_REF = re.compile(
    r"^registry://(?P<tenant>[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?)"
    r"/agents/(?P<namespace>[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)"
    r"/(?P<name>[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)"
    r"@(?P<version>[A-Za-z0-9](?:[-A-Za-z0-9._+]{0,126}[A-Za-z0-9])?)$"
)
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
_DEPENDENCY_FIELDS = {
    "skills": "Skill",
    "tools": "Tool",
    "mcpServers": "MCPServer",
    "prompts": "Prompt",
    "workflows": "Workflow",
    "datasets": "Dataset",
    "evalSuites": "EvalSuite",
}


class AgentImportError(RuntimeError):
    """Base import error translated by the HTTP boundary."""


class AgentImportInvalid(AgentImportError):
    """The requested ref or resolved portable contract is unsafe."""


class AgentImportConflict(AgentImportError):
    """An idempotency key was reused for a different request."""


class AgentImportNotFound(AgentImportError):
    """The import is absent in the authenticated owner scope."""


class AgentImportUnavailable(AgentImportError):
    """Registry or durable storage is unavailable."""


@dataclass(frozen=True, slots=True)
class RegistryAgentRef:
    tenant: str
    namespace: str
    name: str
    version: str

    @property
    def canonical(self) -> str:
        return f"registry://{self.tenant}/agents/{self.namespace}/{self.name}@{self.version}"


class AgentImportDatabase(Protocol):
    async def create_agent_import(self, **values: Any) -> dict[str, Any]: ...

    async def get_agent_import(self, owner_scope: str, import_id: str) -> dict[str, Any] | None: ...

    async def get_agent_import_by_idempotency(
        self,
        owner_scope: str,
        project_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...

    async def list_agent_imports(
        self,
        owner_scope: str,
        project_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...


def parse_registry_agent_ref(value: str) -> RegistryAgentRef:
    """Parse the one portable import grammar; mutable aliases are forbidden."""
    match = _REGISTRY_REF.fullmatch(value.strip())
    if match is None:
        raise AgentImportInvalid("registry_ref must be registry://<tenant>/agents/<namespace>/<name>@<version>")
    ref = RegistryAgentRef(**match.groupdict())
    if ref.version.lower() == "latest":
        raise AgentImportInvalid("registry_ref must use an immutable version; latest is not allowed")
    return ref


def owner_scope(principal: Principal) -> str:
    """Tenant is the sharing/idempotency boundary; local callers remain per-user."""
    scope = principal.tenant_id.strip() or principal.user_scope_id.strip()
    if not scope:
        raise AgentImportInvalid("authenticated principal has no stable tenant or subject")
    return scope


class AgentImportService:
    def __init__(self, *, database: AgentImportDatabase, registry: Any) -> None:
        self._database = database
        self._registry = registry

    async def create(
        self,
        principal: Principal,
        *,
        project_id: str,
        registry_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        scope = owner_scope(principal)
        ref = parse_registry_agent_ref(registry_ref)
        request_fingerprint = _request_fingerprint(project_id, ref.canonical)

        try:
            existing = await self._database.get_agent_import_by_idempotency(
                scope,
                project_id,
                idempotency_key,
            )
        except AttributeError:
            existing = None
        except Exception as exc:  # noqa: BLE001
            raise AgentImportUnavailable("agent import storage unavailable") from exc
        if existing is not None:
            _assert_same_request(existing, request_fingerprint)
            return existing

        try:
            snapshot = await asyncio.to_thread(self._resolve_snapshot, ref)
        except AgentImportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AgentImportUnavailable("Registry resolution unavailable") from exc

        now = datetime.now(UTC)
        values = {
            "id": str(uuid.uuid4()),
            "owner_scope": scope,
            "tenant_id": principal.tenant_id,
            "project_id": project_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "registry_ref": ref.canonical,
            "state": "ready",
            "agent": snapshot["agent"],
            "dependency_lock": snapshot["dependency_lock"],
            "permissions": snapshot["permissions"],
            "conformance": snapshot["conformance"],
            "created_by": principal.user_scope_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            stored = await self._database.create_agent_import(**values)
        except Exception as exc:  # noqa: BLE001
            raise AgentImportUnavailable("agent import storage unavailable") from exc
        _assert_same_request(stored, request_fingerprint)
        return stored

    async def get(self, principal: Principal, import_id: str) -> dict[str, Any]:
        try:
            row = await self._database.get_agent_import(owner_scope(principal), import_id)
        except Exception as exc:  # noqa: BLE001
            raise AgentImportUnavailable("agent import storage unavailable") from exc
        if row is None:
            raise AgentImportNotFound(f"agent import {import_id} not found")
        return row

    async def list(
        self,
        principal: Principal,
        *,
        project_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            return await self._database.list_agent_imports(
                owner_scope(principal),
                project_id,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise AgentImportUnavailable("agent import storage unavailable") from exc

    def _resolve_snapshot(self, ref: RegistryAgentRef) -> dict[str, Any]:
        resolved: ResolvedAgent = self._registry.resolve_agent(
            ref.name,
            namespace=ref.namespace,
            tag=ref.version,
        )
        if resolved.unresolved:
            details = ", ".join(f"{item.kind}:{item.ref}" for item in resolved.unresolved)
            raise AgentImportInvalid(f"agent has unresolved Registry dependencies: {details}")

        envelope = resolved.envelope
        if not isinstance(envelope, dict) or envelope.get("kind") != "Agent":
            raise AgentImportInvalid("Registry returned an invalid Agent envelope")
        metadata = envelope.get("metadata")
        spec = envelope.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise AgentImportInvalid("Registry returned an invalid Agent envelope")
        _assert_agent_identity(ref, metadata)
        _verify_registry_signature(self._registry, metadata)
        runtime = _validate_portable_spec(spec)
        dependency_lock = _build_dependency_lock(spec, resolved.resolved, ref.namespace)

        level = "sandbox_runnable" if runtime["type"] == "container" else "callable"
        return {
            "agent": {
                "kind": "Agent",
                "name": ref.name,
                "namespace": ref.namespace,
                "version": ref.version,
                "digest": metadata["digest"],
                "signature": metadata["signature"],
                "signed_by": metadata["signedBy"],
                "framework": spec["framework"],
                "runtime": runtime,
                "spec": spec,
            },
            "dependency_lock": dependency_lock,
            "permissions": _json_object(spec.get("permissions"), "spec.permissions"),
            "conformance": {
                "level": level,
                "findings": [],
                "evidence": {
                    "registry_signature_verified": True,
                    "dependencies_pinned": True,
                },
            },
        }


def _request_fingerprint(project_id: str, registry_ref: str) -> str:
    canonical = json.dumps(
        {"project_id": project_id, "registry_ref": registry_ref},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _assert_same_request(row: dict[str, Any], fingerprint: str) -> None:
    if row.get("request_fingerprint") != fingerprint:
        raise AgentImportConflict("idempotency key was already used for a different import request")


def _assert_agent_identity(ref: RegistryAgentRef, metadata: dict[str, Any]) -> None:
    actual = (metadata.get("name"), metadata.get("namespace"), metadata.get("tag"))
    expected = (ref.name, ref.namespace, ref.version)
    if actual != expected:
        raise AgentImportInvalid("Registry response identity does not match the requested immutable reference")
    digest = str(metadata.get("digest") or "")
    if _DIGEST.fullmatch(digest) is None:
        raise AgentImportInvalid("Registry Agent has no valid sha256 digest")


def _verify_registry_signature(registry: Any, metadata: dict[str, Any]) -> None:
    signature = str(metadata.get("signature") or "")
    signed_by = str(metadata.get("signedBy") or "")
    if not signature or not signed_by:
        raise AgentImportInvalid("Registry Agent has no verifiable signature")
    try:
        signing_key = registry.get_signing_key()
    except Exception as exc:  # noqa: BLE001
        raise AgentImportUnavailable("Registry signing key unavailable") from exc
    if not isinstance(signing_key, dict) or not signing_key.get("enabled"):
        raise AgentImportInvalid("Registry signing is disabled; import authenticity cannot be verified")
    if signing_key.get("algorithm") != "ed25519" or signing_key.get("signs") != "digest":
        raise AgentImportInvalid("Registry signing key uses an unsupported attestation contract")
    if signing_key.get("keyId") != signed_by:
        raise AgentImportInvalid("Registry Agent signature key does not match the published key")
    try:
        public_bytes = base64.b64decode(str(signing_key.get("publicKey") or ""), validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        key = Ed25519PublicKey.from_public_bytes(public_bytes)
        key.verify(signature_bytes, str(metadata["digest"]).encode())
    except InvalidSignature as exc:
        raise AgentImportInvalid("Registry Agent signature is invalid") from exc
    except (TypeError, ValueError) as exc:
        raise AgentImportInvalid("Registry Agent signature evidence is malformed") from exc


def _validate_portable_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("definitionVersion") != "v1":
        raise AgentImportInvalid("Agent spec.definitionVersion must be v1")
    if not isinstance(spec.get("framework"), str) or not spec["framework"].strip():
        raise AgentImportInvalid("Agent spec.framework is required")
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise AgentImportInvalid("Agent spec.runtime is required")
    runtime_type = runtime.get("type")
    protocol = runtime.get("protocol")
    if protocol not in {"a2a", "http"}:
        raise AgentImportInvalid("Agent runtime protocol must be a2a or http")
    projected: dict[str, Any] = {"type": runtime_type, "protocol": protocol}
    port = runtime.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise AgentImportInvalid("Agent runtime port must be between 1 and 65535")
        projected["port"] = port
    for field in ("path", "healthPath"):
        value = runtime.get(field)
        if value is not None:
            if not _safe_runtime_path(value):
                raise AgentImportInvalid(f"Agent runtime {field} must be an absolute path without traversal")
            projected[field] = value
    if runtime_type == "container":
        image = runtime.get("image")
        if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
            raise AgentImportInvalid("Agent container image must be pinned by sha256 digest")
        if runtime.get("url"):
            raise AgentImportInvalid("Agent container runtime cannot declare a remote URL")
        if runtime.get("auth") is not None:
            raise AgentImportInvalid("Agent container runtime cannot declare authentication")
        projected["image"] = image
        return projected
    if runtime_type == "remote":
        url = runtime.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise AgentImportInvalid("Agent remote runtime must use an absolute HTTPS URL without user info")
        if runtime.get("image"):
            raise AgentImportInvalid("Agent remote runtime cannot declare an image")
        auth = runtime.get("auth")
        if not isinstance(auth, dict) or auth.get("type") != "bearer":
            raise AgentImportInvalid("Agent remote runtime requires bearer authentication")
        credential_ref = auth.get("credentialRef")
        if not isinstance(credential_ref, str) or not credential_ref.strip():
            raise AgentImportInvalid("Agent remote runtime auth.credentialRef is required")
        if set(auth) - {"type", "credentialRef"}:
            raise AgentImportInvalid("Agent remote runtime auth may contain only type and credentialRef")
        projected["url"] = url
        projected["auth"] = {"type": "bearer", "credentialRef": credential_ref}
        return projected
    raise AgentImportInvalid("Agent runtime type must be container or remote")


def _safe_runtime_path(value: Any) -> bool:
    return (
        isinstance(value, str) and value.startswith("/") and not value.startswith("//") and ".." not in value.split("/")
    )


def _build_dependency_lock(
    spec: dict[str, Any],
    resolved: dict[str, list[dict[str, Any]]],
    default_namespace: str,
) -> list[dict[str, str]]:
    lock: list[dict[str, str]] = []
    for field, kind in _DEPENDENCY_FIELDS.items():
        requested = spec.get(field, [])
        if not isinstance(requested, list):
            raise AgentImportInvalid(f"Agent spec.{field} must be a list")
        expected: list[tuple[str, str]] = []
        for index, item in enumerate(requested):
            if not isinstance(item, dict):
                raise AgentImportInvalid(f"Agent spec.{field}[{index}] must be an exact object reference")
            name, version = item.get("ref"), item.get("version")
            if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                raise AgentImportInvalid(f"Agent spec.{field}[{index}] must include ref and version")
            if version.lower() == "latest":
                raise AgentImportInvalid(f"Agent spec.{field}[{index}] uses mutable latest")
            expected.append((name, version))

        entries = resolved.get(field, [])
        if not isinstance(entries, list):
            raise AgentImportInvalid(f"Registry resolved.{field} must be a list")
        actual: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise AgentImportInvalid(f"Registry resolved.{field} contains an invalid artifact")
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                raise AgentImportInvalid(f"Registry resolved.{field} contains an inline dependency")
            name = str(metadata.get("name") or "")
            version = str(metadata.get("tag") or "")
            digest = str(metadata.get("digest") or "")
            namespace = str(metadata.get("namespace") or default_namespace)
            if not name or not version or _DIGEST.fullmatch(digest) is None:
                raise AgentImportInvalid(f"Registry resolved.{field} dependency lacks exact identity or digest")
            actual.append((name, version))
            lock.append(
                {
                    "kind": str(entry.get("kind") or kind),
                    "name": name,
                    "namespace": namespace,
                    "version": version,
                    "digest": digest,
                }
            )
        if actual != expected:
            raise AgentImportInvalid(f"Registry resolved.{field} does not match the requested exact versions")
    return lock


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentImportInvalid(f"{field} must be an object")
    return value


def public_agent_import(row: dict[str, Any]) -> dict[str, Any]:
    """Remove authorization/idempotency internals from an API response."""
    hidden = {"owner_scope", "tenant_id", "idempotency_key", "request_fingerprint"}
    return {key: value for key, value in row.items() if key not in hidden}


__all__ = [
    "AgentImportConflict",
    "AgentImportError",
    "AgentImportInvalid",
    "AgentImportNotFound",
    "AgentImportService",
    "AgentImportUnavailable",
    "RegistryAgentRef",
    "owner_scope",
    "parse_registry_agent_ref",
    "public_agent_import",
]
