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
PRODUCT_MCP_SERVERS = ("homechef-mcp", "kora-mcp", "mark8ly-mcp", "platform-mcp", "stockpilot-mcp")
PRODUCT_GATEWAY_CREDENTIALS = {
    "homechef-mcp": "HOMECHEF_MCP_KEY",
    "kora-mcp": "KORA_MCP_KEY",
    "mark8ly-mcp": "MARK8LY_MCP_KEY",
    "platform-mcp": "PLATFORM_MCP_KEY",
    "stockpilot-mcp": "STOCKPILOT_MCP_KEY",
}
PRODUCT_MCP_TOOLS = {
    "homechef-mcp": {
        "autoGetChefAvailability",
        "autoGetOrder",
        "autoListRecentOrders",
        "autoTrackDelivery",
        "create_refund_request",
        "get_chef_availability",
        "get_order_status",
        "list_recent_orders",
        "lookup_conversation",
        "search_knowledge_base",
        "track_delivery",
    },
    "kora-mcp": {"search_nutrition"},
    "mark8ly-mcp": {
        "check_payment_status",
        "create_refund_request",
        "create_support_ticket",
        "getStoreBranding",
        "getStoreProduct",
        "get_order",
        "listProductsByCategory",
        "listStoreCategories",
        "listStoreProducts",
        "list_recent_orders",
        "list_returns",
        "lookup_conversation",
        "search_knowledge_base",
    },
    "platform-mcp": {
        "get_platform_overview",
        "lookup_conversation",
        "search_knowledge_base",
        "submit_contact_lead",
    },
    "stockpilot-mcp": {
        "autoGetBrokerStatus",
        "autoGetPortfolioSummary",
        "autoListRecentTrades",
        "get_agent_trace",
        "get_broker_status",
        "get_portfolio_summary",
        "list_recent_trades",
        "lookup_conversation",
        "search_knowledge_base",
    },
}
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
        assert manifest["metadata"]["labels"]["mcp.tesserix.app/class"] == "directory", name


def test_routed_servers_declare_modern_protocol_and_explicit_remote() -> None:
    for name in GATEWAY_ROUTED_MCP_SERVERS + PRODUCT_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["apiVersion"] == "registry.agentic.dev/v1alpha1", name
        assert manifest["metadata"]["labels"]["mcp.tesserix.app/class"] == "platform", name
        assert manifest["spec"]["protocolVersion"] == "2026-07-28", name
        assert manifest["spec"]["remotes"] == [{"type": "streamableHttp", "url": manifest["spec"]["endpoint"]}], name


def test_product_mcp_servers_broker_their_upstream_api_keys() -> None:
    for name, key in PRODUCT_GATEWAY_CREDENTIALS.items():
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["authMode"] == "apikey", name
        assert manifest["spec"]["credentialRef"] == {
            "secretName": "product-mcp-upstream-keys",
            "key": key,
            "header": "X-MCP-Key",
        }, name


def test_product_mcp_seeds_declare_the_observed_tool_surface() -> None:
    for name, expected_tools in PRODUCT_MCP_TOOLS.items():
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert set(manifest["spec"]["tools"]) == expected_tools, name


def test_adc_servers_remain_hub_visible_but_are_not_gateway_exported() -> None:
    for name in HUB_ONLY_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["gatewayExport"] is False, name
        assert manifest["metadata"]["labels"]["mcp.tesserix.app/gateway-export"] == "false", name
        assert manifest["metadata"]["labels"]["mcp.tesserix.app/class"] == "directory", name
        assert manifest["spec"].get("catalog") is not True, name


def test_internal_mcp_servers_select_identity_aware_kubernetes_services() -> None:
    for name in INTERNAL_MCP_SERVERS:
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["serviceSelector"] == {
            "namespaces": {"matchLabels": {"kubernetes.io/metadata.name": "devai"}},
            "services": {"matchLabels": {"mcp.tesserix.app/server": name}},
        }, name


def test_external_catalog_is_directory_only_and_protocol_unverified() -> None:
    for path in SEEDS.glob("catalog-*.yaml"):
        manifest = yaml.safe_load(path.read_text())
        labels = manifest["metadata"]["labels"]

        assert labels["mcp.tesserix.app/class"] == "directory", path.name
        assert labels["mcp.tesserix.app/protocol-status"] == "unverified", path.name
        assert "remotes" not in manifest["spec"], path.name
