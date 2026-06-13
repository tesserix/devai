"""Cloud adapter family — GCP / AWS / Azure behind one ABC.

    from devai.adapters.cloud import CloudAdapter, create_cloud_adapter

Backends are lazy-imported by the factory; importing this package loads no
vendor SDK.
"""

from devai.adapters.cloud.base import CloudAdapter
from devai.adapters.cloud.factory import KNOWN_PROVIDERS, create_cloud_adapter
from devai.adapters.cloud.noop import NoopCloudAdapter

__all__ = ["KNOWN_PROVIDERS", "CloudAdapter", "NoopCloudAdapter", "create_cloud_adapter"]
