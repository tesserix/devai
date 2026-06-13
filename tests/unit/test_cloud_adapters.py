"""Cloud adapter family — factory degrade + GCP identity + tool resolution."""

from __future__ import annotations

import json

from devai.adapters.cloud import KNOWN_PROVIDERS, NoopCloudAdapter, create_cloud_adapter
from devai.adapters.cloud.gcp import GcpCloudAdapter


def test_known_providers():
    assert set(KNOWN_PROVIDERS) == {"noop", "gcp", "aws", "azure"}


def test_factory_unknown_degrades_to_noop():
    assert isinstance(create_cloud_adapter({"provider": "wat"}), NoopCloudAdapter)


def test_factory_builds_each_provider():
    assert create_cloud_adapter({"provider": "gcp"}).provider == "gcp"
    assert create_cloud_adapter({"provider": "aws"}).provider == "aws"
    assert create_cloud_adapter({"provider": "azure"}).provider == "azure"


async def test_noop_contract():
    n = NoopCloudAdapter()
    assert (await n.health_check())["ok"] is True
    assert (await n.identity())["ok"] is False
    assert await n.list_scopes() == []


async def test_gcp_identity_from_key():
    key = json.dumps({"client_email": "sa@proj.iam.gserviceaccount.com", "project_id": "proj"})
    a = GcpCloudAdapter(sa_key_json=key)
    ident = await a.identity()
    assert ident["service_account"] == "sa@proj.iam.gserviceaccount.com"
    assert ident["project"] == "proj"


async def test_gcp_identity_bad_key():
    a = GcpCloudAdapter(sa_key_json="not json")
    assert (await a.identity())["ok"] is False


async def test_gcp_no_key():
    a = GcpCloudAdapter()
    assert (await a.identity())["ok"] is False
    assert (await a.list_scopes())[0]["ok"] is False


async def test_cloud_tools_registered():
    from devai.tools import registry as tool_registry

    assert {"cloud_list_accounts", "cloud_identity", "cloud_list_scopes"} <= set(tool_registry.known())


async def test_cloud_tool_requires_identity():
    from devai.tools import registry as tool_registry

    bound = tool_registry.bind(["cloud_identity"], tool_registry.ToolContext(agent_name="t"))  # no triggered_by
    out = await bound[0].handler({})
    assert "no user identity" in out
