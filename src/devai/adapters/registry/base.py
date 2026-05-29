"""Registry adapter family — one shape for every artifact registry backend.

DevAI does not depend on any single registry. It talks to whatever registry the
operator points it at through this adapter family: our own open-source
``agentic-registry`` (provider ``tesserix``), the upstream solo.io aregistry
(``solo_aregistry``), the official MCP Registry (``mcp_registry``), or Portkey
(``portkey``). The registry stays ignorant of DevAI; DevAI swaps backend with
one env var (``DEVAI_REGISTRY_PROVIDER``), exactly like the memory family.

The list/get/publish methods are sync (mirroring ``devai.registry.client`` — the
pipeline calls them via ``asyncio.to_thread``); ``close``/``health_check`` are
async to satisfy the shared :class:`~devai.adapters.base.Adapter` protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Reuse the typed record dataclasses the existing client already defines, so
# every adapter returns the same shapes the pipeline + dashboard expect.
from devai.registry.client import Agent, McpServer, Prompt, Skill


class RegistryAdapter(ABC):
    """Common contract every registry backend implements."""

    provider_name: str = "base"

    # ---- read surface -----------------------------------------------------
    @abstractmethod
    def list_skills(self) -> list[Skill]: ...

    @abstractmethod
    def list_prompts(self) -> list[Prompt]: ...

    @abstractmethod
    def list_mcp_servers(self) -> list[McpServer]: ...

    @abstractmethod
    def list_agents(self) -> list[Agent]: ...

    @abstractmethod
    def get_skill(self, name: str) -> Skill | None: ...

    @abstractmethod
    def get_prompt(self, name: str) -> Prompt | None: ...

    @abstractmethod
    def get_mcp_server(self, name: str) -> McpServer | None: ...

    @abstractmethod
    def get_agent(self, name: str) -> Agent | None: ...

    # ---- label-selector discovery (the gateway-neutral runtime contract) --
    @abstractmethod
    def discover(self, plural: str, label_selector: str = "") -> list[dict[str, Any]]:
        """Return raw artifact records of a kind, filtered by a Kubernetes
        label selector string (e.g. ``"language=go,domain in (code-review)"``).

        Backends that cannot evaluate selectors server-side filter client-side
        or return the unfiltered list; callers treat this as best-effort
        discovery, never an authorization boundary.
        """

    # ---- write surface ----------------------------------------------------
    @abstractmethod
    def publish(self, body: dict[str, Any]) -> dict[str, Any]:
        """Publish a single resource envelope (apiVersion/kind/metadata/spec)."""

    # ---- lifecycle (Adapter protocol) -------------------------------------
    async def close(self) -> None:  # noqa: D401 - protocol method
        """Release resources. Idempotent; default no-op."""

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """``{"ok": bool, "provider": str, "detail": str}`` — never raises."""


__all__ = ["RegistryAdapter", "Skill", "Prompt", "McpServer", "Agent"]
