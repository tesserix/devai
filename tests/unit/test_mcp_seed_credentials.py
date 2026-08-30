from pathlib import Path

import yaml

SEEDS = Path("architecture/registry-seeds/mcp-servers")
GATEWAY_CREDENTIAL = {
    "secretName": "devai-mcp-upstream",
    "key": "token",
    "header": "Authorization",
    "prefix": "Bearer ",
}
INTERNAL_MCP_SERVERS = (
    "analyst-mcp",
    "devai-mcp",
    "gitops-mcp",
    "sample-mcp",
    "scm-mcp",
    "sre-mcp",
)
GATEWAY_ROUTED_MCP_SERVERS = ("gitops-mcp", "sample-mcp", "scm-mcp")
DIRECTORY_ONLY_MCP_SERVERS = ("analyst-mcp", "devai-mcp", "sre-mcp")
HUB_ONLY_MCP_SERVERS = ("google-agent-registry-mcp", "google-vertex-mcp")


def test_internal_mcp_servers_broker_the_devai_service_identity() -> None:
    for name in GATEWAY_ROUTED_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["authMode"] == "jwt", name
        assert manifest["spec"]["credentialRef"] == GATEWAY_CREDENTIAL, name


def test_unimplemented_mcp_endpoints_are_directory_only() -> None:
    for name in DIRECTORY_ONLY_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["catalog"] is True, name
        assert manifest["metadata"]["labels"]["mcp.devai.io/catalog"] == "true", name


def test_adc_servers_remain_hub_visible_but_are_not_gateway_exported() -> None:
    for name in HUB_ONLY_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["gatewayExport"] is False, name
        assert manifest["metadata"]["labels"]["mcp.tesserix.app/gateway-export"] == "false", name
        assert manifest["spec"].get("catalog") is not True, name


def test_internal_mcp_servers_select_identity_aware_kubernetes_services() -> None:
    for name in INTERNAL_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["serviceSelector"] == {
            "namespaces": {"matchLabels": {"kubernetes.io/metadata.name": "devai"}},
            "services": {"matchLabels": {"mcp.tesserix.app/server": name}},
        }, name
