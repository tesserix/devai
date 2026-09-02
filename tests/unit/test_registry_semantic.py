from __future__ import annotations

import json
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.identity import Principal
from devai.mcphub.hub import MCPHub
from devai.mcphub.profile import ToolProfile
from devai.registry.routes import router
from devai.registry.semantic import RegistrySemanticSearch, principal_owner_id
from devai.sandbox.gateway import is_side_effecting


class _Registry:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, tuple[str, ...], int]] = []

    def search_capabilities(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        selected = tuple(kinds or ())
        self.search_calls.append((query, selected, limit))
        hits = [
            {
                "kind": "Tool",
                "name": "analyst-security-scan-sast",
                "namespace": "devai",
                "tag": "v2",
                "arn": "arn:agentic:devai:Tool:analyst-security-scan-sast",
                "digest": "sha256:abc",
                "title": "SAST Scanner",
                "description": "Static application security scanner",
                "visibility": "public",
                "labels": {
                    "devai.io/domain": "quality",
                    "mcp.devai.io/server": "analyst-mcp",
                },
                "annotations": {
                    "mcp.devai.io/wire-name": "security_scan_sast",
                    "devai.io/api-key": "***",
                },
                "attributes": {
                    "tags": ["security", "quality"],
                    "inputSchema": {
                        "properties": {"repository": {"type": "string", "description": "Repository to scan"}}
                    },
                },
                "fetchPath": "/v0/tools/analyst-security-scan-sast/v2?namespace=devai",
            }
        ]
        return [hit for hit in hits if not selected or "tools" in selected][:limit]

    def get_artifact_envelope(self, plural: str, name: str, tag: str = "") -> dict[str, Any] | None:
        if plural != "tools" or name != "analyst-security-scan-sast" or tag not in ("", "v2"):
            return None
        return {
            "kind": "Tool",
            "metadata": {"name": name, "tag": tag or "v2", "visibility": "public"},
            "spec": {"description": "Static application security scanner"},
        }


class _PrivateRegistry(_Registry):
    def __init__(self) -> None:
        super().__init__()
        alice = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")
        self.alice_owner = principal_owner_id(alice)

    def search_capabilities(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        selected = tuple(kinds or ())
        self.search_calls.append((query, selected, limit))
        if selected and "agents" not in selected:
            return []
        return [
            {
                "kind": "Agent",
                "name": "private-terraform-agent",
                "namespace": "tenant-a",
                "tag": "1",
                "title": "Private Terraform Agent",
                "description": "Provisions confidential tenant infrastructure",
                "visibility": "private",
                "labels": {"devai.tesserix.app/owner-id": self.alice_owner},
                "fetchPath": "/v0/agents/private-terraform-agent/1?namespace=tenant-a",
            }
        ][:limit]


def _headers(user: str, tenant: str) -> dict[str, str]:
    return {
        "x-forwarded-user": f"{user}@example.com",
        "x-forwarded-uid": user,
        "x-forwarded-tenant": tenant,
    }


async def test_search_delegates_to_registry_native_index_and_preserves_safe_metadata() -> None:
    registry = _Registry()
    search = RegistrySemanticSearch(registry)

    response = await search.search("code quality", kinds=["tools"], limit=5)

    assert [(hit.kind, hit.name, hit.rank) for hit in response.hits] == [("tools", "analyst-security-scan-sast", 1)]
    hit = response.hits[0]
    assert hit.labels["mcp.devai.io/server"] == "analyst-mcp"
    assert hit.annotations["mcp.devai.io/wire-name"] == "security_scan_sast"
    assert hit.annotations["devai.io/api-key"] == "***"
    assert hit.attributes["inputSchema"]["properties"]["repository"]["description"] == "Repository to scan"
    assert hit.registry_fetch_path == "/v0/tools/analyst-security-scan-sast/v2?namespace=devai"
    assert registry.search_calls == [("code quality", ("tools",), 15)]


def test_search_route_never_returns_another_tenants_private_capability() -> None:
    registry = _PrivateRegistry()
    app = FastAPI()
    app.state.config = SimpleNamespace(auth_bff_shared_secret="", trust_forwarded_without_secret=True)
    app.state.registry_client = registry
    app.include_router(router)
    client = TestClient(app)

    bob = client.get(
        "/api/registry/search",
        params={"q": "confidential tenant infrastructure", "kinds": "agents"},
        headers=_headers("bob", "tenant-b"),
    )
    alice = client.get(
        "/api/registry/search",
        params={"q": "confidential tenant infrastructure", "kinds": "agents"},
        headers=_headers("alice", "tenant-a"),
    )

    assert bob.status_code == 200, bob.text
    assert bob.json()["hits"] == []
    assert alice.status_code == 200, alice.text
    assert [hit["name"] for hit in alice.json()["hits"]] == ["private-terraform-agent"]


def test_search_hit_fetch_path_resolves_the_exact_registry_version() -> None:
    registry = _Registry()
    app = FastAPI()
    app.state.config = SimpleNamespace(auth_bff_shared_secret="", trust_forwarded_without_secret=True)
    app.state.registry_client = registry
    app.include_router(router)
    client = TestClient(app)

    search = client.get("/api/registry/search", params={"q": "static security scanner", "kinds": "tools"})
    fetch = client.get(search.json()["hits"][0]["fetch_path"])

    assert fetch.status_code == 200, fetch.text
    assert fetch.json()["metadata"] == {
        "name": "analyst-security-scan-sast",
        "tag": "v2",
        "visibility": "public",
    }


async def test_mcp_hub_exposes_registry_search_as_a_read_only_control_tool() -> None:
    registry = _Registry()
    search = RegistrySemanticSearch(registry)
    hub = MCPHub(registry, capability_search=search)

    names = {tool.name for tool in hub.list_tools(ToolProfile.default()).selected}
    result = await hub.call_tool(
        "devai__registry_search",
        {"query": "static application security", "kinds": ["tools"], "limit": 3},
    )

    assert "devai__registry_search" in names
    assert not is_side_effecting("devai__registry_search")
    payload = json.loads(result)
    assert [hit["name"] for hit in payload["hits"]] == ["analyst-security-scan-sast"]
    assert payload["provider"] == "agentic-registry"
