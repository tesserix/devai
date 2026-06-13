"""Per-user infrastructure connections: cluster resolution + kubectl targeting.

Proves a user's connected Kubernetes cluster (Settings → Kubernetes Cluster)
resolves with its secret values joined back from GCP SM, and that the gitops
adapters build the right kubectl flags for it — the spine of "agents/MCP can
operate against the user's own clusters".
"""

from __future__ import annotations

import base64

import pytest

from devai.adapters.gitops.base import cluster_kubectl_flags
from devai.settings.connections import user_cluster, user_cluster_names
from devai.settings.models import Scope
from devai.settings.service import SettingsService


class _FakeSecrets:
    """Minimal SecretsAdapter: stores values, returns refs, resolves by name."""

    provider_name = "fake"

    def __init__(self):
        self.store: dict[str, str] = {}

    async def can_write(self) -> bool:
        return True

    async def set_secret(self, logical, value, labels=None):
        from types import SimpleNamespace

        self.store[logical] = value
        return SimpleNamespace(name=logical)

    async def get_secret(self, name):
        return self.store.get(name)


@pytest.fixture
def svc():
    return SettingsService(pool=None, secrets=_FakeSecrets())


async def _add_cluster(svc, email, name, server, token, ca=""):
    await svc.upsert_connector(
        scope=Scope.USER,
        scope_id=email,
        connector_key="kubernetes",
        provider="gke",
        instance_id=name,
        prefs={"k8s_name": name, "k8s_api_server": server, "k8s_namespace": "apps"},
        secret_values={"k8s_token": token, "k8s_ca_cert": ca},
        updated_by=email,
    )


# ── flags ────────────────────────────────────────────────────────────────


def test_flags_in_cluster_is_empty():
    assert cluster_kubectl_flags(None) == []
    assert cluster_kubectl_flags({}) == []


def test_flags_with_ca_writes_cert_file():
    ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nXYZ\n-----END CERTIFICATE-----").decode()
    flags = cluster_kubectl_flags({"server": "https://h:6443", "token": "t", "ca_data": ca})
    assert "--server=https://h:6443" in flags
    assert "--token=t" in flags
    assert any(f.startswith("--certificate-authority=") for f in flags)
    assert not any("insecure" in f for f in flags)


def test_flags_without_ca_is_insecure():
    flags = cluster_kubectl_flags({"server": "https://h:6443", "token": "t"})
    assert "--insecure-skip-tls-verify=true" in flags


# ── resolution ───────────────────────────────────────────────────────────


async def test_user_cluster_resolves_secret(svc):
    await _add_cluster(svc, "a@x.com", "prod", "https://prod:6443", "tok-prod")
    c = await user_cluster("a@x.com", "prod", svc=svc)
    assert c is not None
    assert c["server"] == "https://prod:6443"
    assert c["token"] == "tok-prod"  # joined back from the fake SM
    assert c["namespace"] == "apps"


async def test_single_cluster_resolves_without_name(svc):
    await _add_cluster(svc, "a@x.com", "only", "https://only:6443", "tok")
    c = await user_cluster("a@x.com", "", svc=svc)
    assert c is not None and c["name"] == "only"


async def test_ambiguous_clusters_need_a_name(svc):
    await _add_cluster(svc, "a@x.com", "prod", "https://prod:6443", "t1")
    await _add_cluster(svc, "a@x.com", "stg", "https://stg:6443", "t2")
    assert await user_cluster("a@x.com", "", svc=svc) is None  # ambiguous
    assert (await user_cluster("a@x.com", "stg", svc=svc))["server"] == "https://stg:6443"
    assert set(await user_cluster_names("a@x.com", svc=svc)) == {"prod", "stg"}


async def test_isolation_other_user_sees_nothing(svc):
    await _add_cluster(svc, "a@x.com", "prod", "https://prod:6443", "secret-tok")
    assert await user_cluster("b@y.com", "prod", svc=svc) is None
    assert await user_cluster_names("b@y.com", svc=svc) == []


async def test_unknown_email_returns_none(svc):
    assert await user_cluster("not-an-email", "prod", svc=svc) is None
