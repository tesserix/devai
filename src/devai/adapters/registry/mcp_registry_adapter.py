"""MCP Registry adapter — speaks the official ``/v0.1`` Generic Registry API.

Targets any registry implementing the MCP Registry contract (the official
registry.modelcontextprotocol.io, our own ``agentic-registry`` on its
``/v0.1`` surface, or any compatible aggregator). It only carries MCP servers;
the other kinds return empty lists.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.registry.base import Agent, McpServer, Prompt, RegistryAdapter, Skill

logger = logging.getLogger(__name__)


class MCPRegistryAdapter(RegistryAdapter):
    provider_name = "mcp_registry"

    def __init__(self, base_url: str, token: str = "", timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _get(self, path: str) -> dict[str, Any]:
        import httpx

        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        r = httpx.get(f"{self._base}{path}", headers=headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def list_mcp_servers(self) -> list[McpServer]:
        try:
            data = self._get("/v0.1/servers")
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp_registry list servers failed: %s", e)
            return []
        out: list[McpServer] = []
        for s in data.get("servers", []):
            out.append(
                McpServer(
                    name=s.get("name", ""),
                    type="mcp",
                    description=s.get("description", "") or "",
                    version=str(s.get("version", "")),
                    raw=s,
                )
            )
        return out

    def get_mcp_server(self, name: str) -> McpServer | None:
        for s in self.list_mcp_servers():
            if s.name == name:
                return s
        return None

    def discover(self, plural: str, label_selector: str = "") -> list[dict[str, Any]]:
        if plural not in ("servers", "mcpservers"):
            return []
        try:
            return self._get("/v0.1/servers").get("servers", [])
        except Exception:  # noqa: BLE001
            return []

    def publish(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        r = httpx.post(f"{self._base}/v0.1/publish", json=body.get("spec", body), headers=headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json() if r.text else {}

    # MCP registry carries only servers.
    def list_skills(self) -> list[Skill]:
        return []

    def list_prompts(self) -> list[Prompt]:
        return []

    def list_agents(self) -> list[Agent]:
        return []

    def get_skill(self, name: str) -> Skill | None:
        return None

    def get_prompt(self, name: str) -> Prompt | None:
        return None

    def get_agent(self, name: str) -> Agent | None:
        return None

    async def health_check(self) -> dict[str, Any]:
        try:
            self._get("/v0.1/health")
            return {"ok": True, "provider": self.provider_name, "detail": "reachable"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}
