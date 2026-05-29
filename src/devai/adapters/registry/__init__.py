"""Registry adapter family.

DevAI talks to whatever artifact registry the operator configures —  our own
open-source ``agentic-registry`` (default), solo.io aregistry, the official MCP
Registry, or Portkey — through a single swappable interface. Select with
``DEVAI_REGISTRY_PROVIDER``; the registry itself never depends on DevAI.

    from devai.adapters.registry import create_registry_adapter

    adapter = create_registry_adapter(settings)
    skills = adapter.list_skills()
    go_review = adapter.discover("skills", "language=go,domain in (code-review)")
"""

from __future__ import annotations

from devai.adapters.registry.base import Agent, McpServer, Prompt, RegistryAdapter, Skill
from devai.adapters.registry.factory import (
    KNOWN_PROVIDERS,
    create_registry_adapter,
    registry_registry,
)

__all__ = [
    "RegistryAdapter",
    "Skill",
    "Prompt",
    "McpServer",
    "Agent",
    "KNOWN_PROVIDERS",
    "create_registry_adapter",
    "registry_registry",
]
