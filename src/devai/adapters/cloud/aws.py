"""AWS cloud backend — STS identity via boto3 (lazy).

Uses the connector's access key / secret to resolve the caller identity and
the account id. boto3 is imported lazily; absent → a clear degrade, not a
crash.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from devai.adapters.cloud.base import CloudAdapter, err

logger = logging.getLogger(__name__)


class AwsCloudAdapter(CloudAdapter):
    provider = "aws"

    def __init__(self, *, region: str = "", access_key_id: str = "", secret_access_key: str = "") -> None:
        self._region = region or "us-east-1"
        self._akid = access_key_id
        self._secret = secret_access_key

    def _client(self, service: str) -> Any:
        import boto3  # lazy

        return boto3.client(
            service,
            region_name=self._region,
            aws_access_key_id=self._akid or None,
            aws_secret_access_key=self._secret or None,
        )

    async def identity(self) -> dict[str, Any]:
        if not self._akid:
            return err("aws: no access key on the connector")
        try:
            sts = self._client("sts")
            ident = await asyncio.to_thread(sts.get_caller_identity)
        except Exception as e:  # noqa: BLE001 — incl. missing boto3
            return err(f"aws: identity failed: {e}")
        return {
            "provider": "aws",
            "account": ident.get("Account", ""),
            "arn": ident.get("Arn", ""),
            "region": self._region,
        }

    async def list_scopes(self) -> list[dict[str, Any]]:
        ident = await self.identity()
        if ident.get("ok", True) is False:
            return [ident]
        return [{"id": ident.get("account", ""), "name": ident.get("arn", ""), "region": self._region}]


__all__ = ["AwsCloudAdapter"]
