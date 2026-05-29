"""Portkey registry adapter — treats Portkey's prompt library as the catalog.

Portkey's control plane is proprietary and oriented around prompt rendering, so
this adapter mainly bridges prompt fetch/render; the other artifact kinds return
empty lists. It exists so a DevAI deployment already invested in Portkey can use
it as the prompt registry without changing pipeline code. Provider secrets stay
in the secret manager — only the API key reference is read from settings.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.registry.base import Agent, McpServer, Prompt, RegistryAdapter, Skill

logger = logging.getLogger(__name__)

_PORTKEY_BASE = "https://api.portkey.ai/v1"


class PortkeyRegistryAdapter(RegistryAdapter):
    provider_name = "portkey"

    def __init__(self, api_key: str, base_url: str = _PORTKEY_BASE, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def render_prompt(self, prompt_id: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Render a Portkey prompt to a ready-to-send completion object."""
        import httpx

        r = httpx.post(
            f"{self._base}/prompts/{prompt_id}/render",
            json={"variables": variables},
            headers={"x-portkey-api-key": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    # Portkey does not expose a generic catalog list API; these are empty.
    def list_skills(self) -> list[Skill]:
        return []

    def list_prompts(self) -> list[Prompt]:
        return []

    def list_mcp_servers(self) -> list[McpServer]:
        return []

    def list_agents(self) -> list[Agent]:
        return []

    def get_skill(self, name: str) -> Skill | None:
        return None

    def get_prompt(self, name: str) -> Prompt | None:
        return None

    def get_mcp_server(self, name: str) -> McpServer | None:
        return None

    def get_agent(self, name: str) -> Agent | None:
        return None

    def discover(self, plural: str, label_selector: str = "") -> list[dict[str, Any]]:
        return []

    def publish(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"published": False, "reason": "portkey adapter is read/render-only"}

    async def health_check(self) -> dict[str, Any]:
        ok = bool(self._api_key)
        return {"ok": ok, "provider": "portkey", "detail": "api key set" if ok else "no api key"}
