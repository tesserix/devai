"""No-op registry adapter — the safe fallback when no registry is configured.

Returns empty catalogs so the pipeline degrades to its in-tree YAML specs
rather than crashing. Mirrors the memory family's NoopMemoryAdapter.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.registry.base import Agent, McpServer, Prompt, RegistryAdapter, Skill


class NoopRegistryAdapter(RegistryAdapter):
    provider_name = "noop"

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
        return {"published": False, "reason": "registry disabled (noop)"}

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": "noop", "detail": "registry disabled"}
