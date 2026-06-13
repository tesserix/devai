"""Factory — build a CloudAdapter from a resolved connector dict.

``create_cloud_adapter(conn)`` reads a per-user Cloud Account connector
(Settings → Cloud Account, secrets already joined from GCP SM) and returns
the right backend. Unknown provider / build failure → Noop. Never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.cloud.base import CloudAdapter
from devai.adapters.cloud.noop import NoopCloudAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "gcp", "aws", "azure")


def create_cloud_adapter(conn: dict[str, Any]) -> CloudAdapter:
    """Build a backend for one resolved cloud connector dict.

    ``conn`` carries ``{provider, gcp_project_id, gcp_sa_key, aws_region,
    aws_access_key_id, aws_secret_access_key, azure_*}`` — whatever the
    selected provider needs. Missing creds still build (the adapter degrades
    on call), so a half-configured connector doesn't crash discovery.
    """
    provider = str(conn.get("provider") or "noop").strip().lower()
    try:
        if provider == "gcp":
            from devai.adapters.cloud.gcp import GcpCloudAdapter

            return GcpCloudAdapter(
                project_id=str(conn.get("gcp_project_id") or ""),
                sa_key_json=str(conn.get("gcp_sa_key") or ""),
            )
        if provider == "aws":
            from devai.adapters.cloud.aws import AwsCloudAdapter

            return AwsCloudAdapter(
                region=str(conn.get("aws_region") or ""),
                access_key_id=str(conn.get("aws_access_key_id") or ""),
                secret_access_key=str(conn.get("aws_secret_access_key") or ""),
            )
        if provider == "azure":
            from devai.adapters.cloud.azure import AzureCloudAdapter

            return AzureCloudAdapter(
                subscription_id=str(conn.get("azure_subscription_id") or ""),
                tenant_id=str(conn.get("azure_tenant_id") or ""),
                client_id=str(conn.get("azure_client_id") or ""),
                client_secret=str(conn.get("azure_client_secret") or ""),
            )
    except Exception:  # noqa: BLE001 — degrade, don't crash
        logger.warning("cloud: provider %r failed to build — using noop", provider, exc_info=True)
        return NoopCloudAdapter()
    if provider != "noop":
        logger.warning("cloud: unknown provider %r — using noop", provider)
    return NoopCloudAdapter()


__all__ = ["KNOWN_PROVIDERS", "create_cloud_adapter"]
