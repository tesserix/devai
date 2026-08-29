"""OpenBao reader with workload-authenticated blind writes through secret-service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from devai.adapters.base import AdapterError, AdapterNotConfigured
from devai.adapters.secrets.base import SecretRef, SecretsAdapter

logger = logging.getLogger(__name__)

_DEFAULT_KUBERNETES_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_DEFAULT_BROKER_TOKEN = "/var/run/secrets/devai/secret-service/token"
_PREFIX = "devai/devai-api"
_SECRET_NAME = re.compile(r"[^a-z0-9-]+")


def _secret_name(key: str) -> str:
    name = _SECRET_NAME.sub("-", key.lower()).strip("-")[:128]
    return name or "secret"


def _owner(labels: dict[str, str] | None, key: str) -> str:
    metadata = labels or {}
    source = f"{metadata.get('scope', '')}:{metadata.get('scope_id', '')}".strip(":") or key
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


class OpenBaoSecretsAdapter(SecretsAdapter):
    provider_name = "openbao"

    def __init__(self, settings: Any, *, client: httpx.AsyncClient | None = None) -> None:
        self._addr = str(getattr(settings, "secrets_openbao_addr", "") or "").rstrip("/")
        self._mount = str(getattr(settings, "secrets_openbao_mount", "kv") or "kv").strip("/")
        self._role = str(getattr(settings, "secrets_openbao_role", "read-devai-api") or "")
        self._auth_mount = str(getattr(settings, "secrets_openbao_auth_mount", "kubernetes") or "kubernetes").strip("/")
        self._token_file = Path(
            str(getattr(settings, "secrets_openbao_token_file", _DEFAULT_KUBERNETES_TOKEN) or _DEFAULT_KUBERNETES_TOKEN)
        )
        self._broker = str(getattr(settings, "secrets_broker_url", "") or "").rstrip("/")
        self._broker_token_file = Path(
            str(getattr(settings, "secrets_broker_token_file", _DEFAULT_BROKER_TOKEN) or _DEFAULT_BROKER_TOKEN)
        )
        if not self._addr or not self._role or not self._broker:
            raise AdapterNotConfigured(
                "openbao secrets adapter requires DEVAI_SECRETS_OPENBAO_ADDR, "
                "DEVAI_SECRETS_OPENBAO_ROLE and DEVAI_SECRETS_BROKER_URL"
            )
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._owns_client = client is None
        self._bao_token = ""
        self._bao_token_expires = 0.0
        self._auth_lock = asyncio.Lock()

    async def _read_token(self, path: Path) -> str:
        try:
            token = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
        except OSError as error:
            raise AdapterError(f"cannot read projected workload token at {path}") from error
        if not token:
            raise AdapterError(f"projected workload token at {path} is empty")
        return token

    async def _broker_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._read_token(self._broker_token_file)}"}

    async def _openbao_token(self) -> str:
        if self._bao_token and time.monotonic() < self._bao_token_expires:
            return self._bao_token
        async with self._auth_lock:
            if self._bao_token and time.monotonic() < self._bao_token_expires:
                return self._bao_token
            jwt = await self._read_token(self._token_file)
            response = await self._client.post(
                f"{self._addr}/v1/auth/{self._auth_mount}/login",
                json={"jwt": jwt, "role": self._role},
            )
            response.raise_for_status()
            auth = response.json().get("auth") or {}
            token = str(auth.get("client_token") or "")
            if not token:
                raise AdapterError("OpenBao Kubernetes login returned no client token")
            lease = max(60, int(auth.get("lease_duration") or 600))
            self._bao_token = token
            self._bao_token_expires = time.monotonic() + max(30, lease - 60)
            return token

    @staticmethod
    def _parts(ref: SecretRef | str) -> tuple[str, str, str]:
        name = ref.name if isinstance(ref, SecretRef) else str(ref)
        parts = name.split("/")
        if len(parts) != 4 or "/".join(parts[:2]) != _PREFIX:
            raise AdapterError("OpenBao secret reference is outside the DevAI prefix")
        owner, secret = parts[2], parts[3]
        if not re.fullmatch(r"[a-f0-9]{32}", owner) or secret != _secret_name(secret):
            raise AdapterError("OpenBao secret reference is malformed")
        return name, owner, secret

    async def can_write(self) -> bool:
        try:
            response = await self._client.get(
                f"{self._broker}/internal/v1/workload-secrets/capabilities",
                headers=await self._broker_headers(),
            )
            return response.status_code == httpx.codes.OK and bool(response.json().get("write"))
        except (AdapterError, httpx.HTTPError, ValueError):
            logger.warning("openbao secret broker capability check failed", exc_info=True)
            return False

    async def set_secret(
        self,
        key: str,
        value: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> SecretRef:
        owner, secret = _owner(labels, key), _secret_name(key)
        try:
            response = await self._client.put(
                f"{self._broker}/internal/v1/workload-secrets/{owner}/{secret}",
                headers=await self._broker_headers(),
                json={"value": value},
            )
            response.raise_for_status()
            version = str(response.json().get("version") or "latest")
        except (httpx.HTTPError, ValueError, AdapterError) as error:
            raise AdapterError(f"OpenBao broker write failed for {owner}/{secret}") from error
        return SecretRef(
            name=f"{_PREFIX}/{owner}/{secret}",
            provider=self.provider_name,
            version=version,
            labels=dict(labels or {}),
        )

    async def get_secret(self, ref: SecretRef | str) -> str | None:
        name, _, _ = self._parts(ref)
        try:
            token = await self._openbao_token()
            response = await self._client.get(
                f"{self._addr}/v1/{self._mount}/data/{name}",
                headers={"X-Vault-Token": token},
            )
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            response.raise_for_status()
            value = ((response.json().get("data") or {}).get("data") or {}).get("value")
            return str(value) if value is not None else None
        except (httpx.HTTPError, ValueError, AdapterError):
            logger.warning("openbao secret read failed for %s", name, exc_info=True)
            return None

    async def delete_secret(self, ref: SecretRef | str) -> bool:
        _, owner, secret = self._parts(ref)
        try:
            response = await self._client.delete(
                f"{self._broker}/internal/v1/workload-secrets/{owner}/{secret}",
                headers=await self._broker_headers(),
            )
            if response.status_code not in (httpx.codes.NO_CONTENT, httpx.codes.NOT_FOUND):
                response.raise_for_status()
            return True
        except (httpx.HTTPError, AdapterError):
            logger.warning("openbao secret broker delete failed for %s/%s", owner, secret, exc_info=True)
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health_check(self) -> dict[str, Any]:
        writable = await self.can_write()
        return {
            "ok": writable,
            "provider": self.provider_name,
            "detail": "blind writes via secret-service; read-only OpenBao workload policy",
        }


__all__ = ["OpenBaoSecretsAdapter"]
