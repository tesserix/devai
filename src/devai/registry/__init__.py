"""DevAI runtime client for the Solo.io agent registry (aregistry).

The catalog (skills + prompts + MCP servers + agents) lives in
``aregistry`` running at ``http://agentregistry.agentregistry-system.svc.cluster.local:12121``.
DevAI's pipeline + chat + dashboard all read from this client when they
need to know "what specializations / tools / agents are available".

Design points:

* HTTP client is the upstream aregistry v0 surface. We don't talk to
  agentregistry's gRPC port; one wire format is enough.
* ``RegistryClient`` holds a small in-memory TTL cache (30s default)
  so a dashboard page render isn't 4 round-trips.
* Every read returns a typed list/dict; no raw JSON leaks into callers.
* On failure (network, 5xx, schema mismatch) the client returns ``[]``
  and emits a structured log. Specialization loader has its own local-
  YAML fallback for the runtime case, so a registry blip never wedges
  a pipeline.
* Optional bearer auth — when the cluster runs aregistry with
  anonymous-auth enabled (today's prod config), the token is empty
  and the client sends no Authorization header.

Public surface:

    RegistryClient                — HTTP client; instantiate per process
    Skill, Prompt, McpServer,
    Agent                         — typed record dataclasses
    create_registry_client(settings) — factory; reads DEVAI_REGISTRY_*

The factory's signature mirrors the adapter family — same lazy / never-
raise discipline.
"""

from devai.registry.client import (
    Agent,
    McpServer,
    Prompt,
    RegistryClient,
    RegistryError,
    Skill,
    create_registry_client,
)

__all__ = [
    "Agent",
    "McpServer",
    "Prompt",
    "RegistryClient",
    "RegistryError",
    "Skill",
    "create_registry_client",
]
