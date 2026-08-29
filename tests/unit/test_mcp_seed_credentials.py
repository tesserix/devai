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


def test_internal_mcp_servers_broker_the_devai_service_identity() -> None:
    for name in ("devai-mcp", "analyst-mcp", "sre-mcp"):
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["credentialRef"] == GATEWAY_CREDENTIAL, name


def test_internal_mcp_servers_select_identity_aware_kubernetes_services() -> None:
    for name in INTERNAL_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["serviceSelector"] == {
            "namespaces": {"matchLabels": {"kubernetes.io/metadata.name": "devai"}},
            "services": {"matchLabels": {"mcp.tesserix.app/server": name}},
        }, name
