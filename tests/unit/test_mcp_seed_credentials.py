from pathlib import Path

import yaml

SEEDS = Path("architecture/registry-seeds/mcp-servers")
GATEWAY_CREDENTIAL = {
    "secretName": "devai-mcp-upstream",
    "key": "token",
    "header": "Authorization",
    "prefix": "Bearer ",
}


def test_internal_mcp_servers_broker_the_devai_service_identity() -> None:
    for name in ("devai-mcp", "analyst-mcp", "sre-mcp"):
        manifest = yaml.safe_load((SEEDS / f"{name}.yaml").read_text())

        assert manifest["spec"]["credentialRef"] == GATEWAY_CREDENTIAL, name
