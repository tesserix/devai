"""DevAI Python SDK — embed DevAI in other apps.

Import this package when you want to talk to the DevAI control plane
(catalog, pipeline, agents, memory) from outside DevAI's own webhook
server. Everything is sync-friendly + lazy-imports the heavy
dependencies (httpx, the LLM/memory adapters) so a slim install
that only needs the registry catalog never loads the rest.

Quick start::

    from devai.sdk import DevAI

    devai = DevAI(
        registry_url="http://agentregistry.agentregistry-system.svc.cluster.local:12121",
        api_url="http://devai-api.devai.svc.cluster.local:8080",
    )

    # Browse the catalog
    for s in devai.skills():
        print(s.name, s.category)

    # Look up an agent and resolve its prompt + skills
    agent = devai.agents().by_name("ci-monitor-agent")
    prompt = devai.prompt(agent.prompts[0]) if agent.prompts else None

    # Kick off a pipeline run
    run = devai.pipeline.dispatch(
        blueprint="default",
        intent="Add a /health endpoint to the api service",
        repo="tesserix/devai",
    )
    for event in devai.pipeline.stream(run.id):
        print(event)

The SDK is intentionally a thin facade over the existing modules:

  * ``DevAI.registry`` → :class:`devai.registry.RegistryClient`
  * ``DevAI.pipeline`` → :class:`devai.sdk.pipeline.PipelineSDK`
  * ``DevAI.specializations`` → :class:`devai.sdk.specializations.SpecializationsSDK`

Public surface:

    DevAI                  — the entry point (constructor + properties)
    Skill / Prompt /
    McpServer / Agent      — re-exported from devai.registry
    AgentRun               — pipeline run handle
    DevAIError             — base exception
"""

from devai.sdk.client import DevAI, DevAIError
from devai.registry import Agent, McpServer, Prompt, Skill

__all__ = [
    "Agent",
    "DevAI",
    "DevAIError",
    "McpServer",
    "Prompt",
    "Skill",
]
