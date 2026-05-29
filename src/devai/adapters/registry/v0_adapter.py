"""V0 registry adapter — wraps the catalog ``/v0/*`` API.

Both our own ``agentic-registry`` (provider ``tesserix``) and the upstream
solo.io aregistry (provider ``solo_aregistry``) speak the same ``/v0/*``
contract, so they share this adapter; only the base URL differs. Our registry
additionally supports the ``labelSelector`` query param, which :meth:`discover`
uses — solo's aregistry ignores it and returns the full list (best-effort).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from devai.adapters.registry.base import Agent, McpServer, Prompt, RegistryAdapter, Skill
from devai.registry.client import RegistryClient

logger = logging.getLogger(__name__)


class V0RegistryAdapter(RegistryAdapter):
    """Adapter over the shared ``/v0/*`` catalog client."""

    def __init__(self, client: RegistryClient, *, provider_name: str = "tesserix") -> None:
        self._client = client
        self.provider_name = provider_name

    # ---- read -------------------------------------------------------------
    def list_skills(self) -> list[Skill]:
        return self._client.list_skills()

    def list_prompts(self) -> list[Prompt]:
        return self._client.list_prompts()

    def list_mcp_servers(self) -> list[McpServer]:
        return self._client.list_mcp_servers()

    def list_agents(self) -> list[Agent]:
        return self._client.list_agents()

    def get_skill(self, name: str) -> Skill | None:
        return self._client.get_skill(name)

    def get_prompt(self, name: str) -> Prompt | None:
        return self._client.get_prompt(name)

    def get_mcp_server(self, name: str) -> McpServer | None:
        return self._client.get_mcp_server(name)

    def get_agent(self, name: str) -> Agent | None:
        return self._client.get_agent(name)

    # ---- discovery --------------------------------------------------------
    def discover(self, plural: str, label_selector: str = "") -> list[dict[str, Any]]:
        path = f"/v0/{plural}"
        if label_selector:
            path += f"?labelSelector={quote(label_selector)}"
        try:
            data = self._client._get(path)  # noqa: SLF001 - intentional reuse
        except Exception as e:  # noqa: BLE001
            logger.warning("registry discover %s failed: %s", path, e)
            return []
        if isinstance(data, list):
            return data
        # tolerate an envelope {"<plural>": [...]}
        items = data.get(plural) if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    # ---- write ------------------------------------------------------------
    def publish(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind", "")).lower()
        plural = {
            "skill": "skills",
            "tool": "tools",
            "mcpserver": "mcpservers",
            "prompt": "prompts",
            "workflow": "workflows",
            "blueprint": "blueprints",
            "agent": "agents",
        }.get(kind, "skills")
        return self._client._post(f"/v0/{plural}", body)  # noqa: SLF001

    # ---- lifecycle --------------------------------------------------------
    async def health_check(self) -> dict[str, Any]:
        try:
            h = self._client.health()
            ok = bool(h.get("reachable"))
            return {"ok": ok, "provider": self.provider_name, "detail": h.get("status", "")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}
