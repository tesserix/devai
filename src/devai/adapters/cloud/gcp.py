"""GCP cloud backend — service-account key → Resource Manager REST.

Mints an access token from the connector's SA-key JSON (google.oauth2,
lazy-imported) and lists projects via cloudresourcemanager.googleapis.com.
No google client libraries beyond the auth shim; the REST call is httpx.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.adapters.cloud.base import CloudAdapter, err

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpCloudAdapter(CloudAdapter):
    provider = "gcp"

    def __init__(self, *, project_id: str = "", sa_key_json: str = "", timeout: float = 30.0) -> None:
        self._project = project_id
        self._sa_key = sa_key_json
        self._timeout = timeout

    def _credentials(self) -> Any:
        from google.oauth2 import service_account  # lazy

        info = json.loads(self._sa_key)
        return service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])

    async def _token(self) -> str:
        from google.auth.transport.requests import Request  # lazy

        creds = self._credentials()
        creds.refresh(Request())
        return str(creds.token or "")

    async def _get(self, url: str) -> dict[str, Any]:
        import httpx  # lazy

        token = await self._token()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    async def identity(self) -> dict[str, Any]:
        if not self._sa_key:
            return err("gcp: no service-account key on the connector")
        try:
            info = json.loads(self._sa_key)
        except Exception as e:  # noqa: BLE001
            return err(f"gcp: service-account key is not valid JSON: {e}")
        return {
            "provider": "gcp",
            "service_account": info.get("client_email", ""),
            "project": self._project or info.get("project_id", ""),
        }

    async def list_scopes(self) -> list[dict[str, Any]]:
        if not self._sa_key:
            return [err("gcp: no service-account key on the connector")]
        try:
            data = await self._get("https://cloudresourcemanager.googleapis.com/v1/projects")
        except Exception as e:  # noqa: BLE001
            return [err(f"gcp: list projects failed: {e}")]
        return [
            {"id": p.get("projectId", ""), "name": p.get("name", ""), "state": p.get("lifecycleState", "")}
            for p in data.get("projects", []) or []
        ]


__all__ = ["GcpCloudAdapter"]
