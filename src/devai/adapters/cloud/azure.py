"""Azure cloud backend — service-principal → subscriptions REST.

Mints an AAD token from the connector's tenant/client/secret (client-
credentials grant) and lists subscriptions via management.azure.com. httpx
only; no azure SDK required.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.cloud.base import CloudAdapter, err

logger = logging.getLogger(__name__)

_RESOURCE = "https://management.azure.com"


class AzureCloudAdapter(CloudAdapter):
    provider = "azure"

    def __init__(
        self,
        *,
        subscription_id: str = "",
        tenant_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._sub = subscription_id
        self._tenant = tenant_id
        self._client = client_id
        self._secret = client_secret
        self._timeout = timeout

    async def _token(self) -> str:
        import httpx  # lazy

        url = f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client,
            "client_secret": self._secret,
            "scope": f"{_RESOURCE}/.default",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, data=form)
            resp.raise_for_status()
            return str(resp.json().get("access_token", ""))

    async def identity(self) -> dict[str, Any]:
        if not (self._tenant and self._client and self._secret):
            return err("azure: tenant/client/secret incomplete on the connector")
        try:
            token = await self._token()
        except Exception as e:  # noqa: BLE001
            return err(f"azure: token mint failed: {e}")
        return {
            "provider": "azure",
            "tenant": self._tenant,
            "client_id": self._client,
            "subscription": self._sub,
            "authenticated": bool(token),
        }

    async def list_scopes(self) -> list[dict[str, Any]]:
        try:
            token = await self._token()
            import httpx  # lazy

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{_RESOURCE}/subscriptions?api-version=2020-01-01",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            return [err(f"azure: list subscriptions failed: {e}")]
        return [
            {"id": s.get("subscriptionId", ""), "name": s.get("displayName", ""), "state": s.get("state", "")}
            for s in data.get("value", []) or []
        ]


__all__ = ["AzureCloudAdapter"]
