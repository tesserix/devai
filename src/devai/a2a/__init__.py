"""A2A (Agent2Agent protocol) runtime consumer for the DevAI orchestrator.

This is the *client* side of A2A: at runtime a DevAI pod discovers an agent
through the Agentic Registry, fetches its **Agent Card** (the capability
descriptor — what the agent can do, where it lives, how to call it), and then
talks A2A *directly* to the agent's own service. The registry is the
discovery/trust layer only; it never sits on the agent-to-agent request path.

Typical flow inside the orchestrator::

    a2a = create_a2a_client(settings, registry_client)
    # 1. discover by capability (tag/keyword) — or fetch a known agent by name
    cards = a2a.discover(query="kubernetes")          # [AgentCard, ...]
    card = a2a.fetch_card("oncall-responder", namespace="sre")
    # 2. pick a skill and invoke the agent over A2A
    result = a2a.send_message(card, "Pod is CrashLoopBackOff — propose a fix.")

Everything is intentionally tolerant: missing optional fields, unknown future
A2A fields (kept in ``raw``), and a registry that is briefly unreachable all
degrade gracefully rather than crashing the pod.
"""

from devai.a2a.card import AgentCard, AgentSkill
from devai.a2a.client import A2AClient, create_a2a_client, resolve_host_block_check
from devai.a2a.errors import A2AError
from devai.a2a.verify import check_service_url, verify_card_signature

__all__ = [
    "AgentCard",
    "AgentSkill",
    "A2AClient",
    "A2AError",
    "create_a2a_client",
    "resolve_host_block_check",
    "verify_card_signature",
    "check_service_url",
]
