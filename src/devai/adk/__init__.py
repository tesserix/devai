"""DevAI Agent Development Kit (ADK).

The SDK (``devai.sdk``) is for *consuming* DevAI's catalog + pipeline.
The ADK is for *building* new entries in that catalog. Both share the
same registry client under the hood but expose very different surfaces:

================== ===================================================
Audience            Surface
================== ===================================================
SDK                 ``DevAI`` — list/get/dispatch; one entry point.
ADK                 ``Skill / Agent / Prompt / McpServer`` builders +
                    ``Publisher`` — fluent constructors that emit the
                    same JSON aregistry expects, plus a CLI
                    (``devai adk …``) for scaffolding.
================== ===================================================

Typical workflow::

    from devai.adk import Skill, Agent, Publisher

    sk = (
        Skill("my-skill")
        .title("My Skill")
        .description("Does the thing")
        .category("review")
        .version("1")
    )

    ag = (
        Agent("my-agent")
        .description("Drives my-skill")
        .skill("my-skill")
        .prompt("my-skill-prompt-v1")
        .model("anthropic", "claude-sonnet-4-20250514")
        .image("ghcr.io/tesserix/devai/devai:latest")
    )

    pub = Publisher(registry_url="http://aregistry:12121")
    pub.publish(sk)
    pub.publish(ag)
    print(pub.summary())

The ADK keeps the on-the-wire body identical to what
``devai-registry-bootstrap`` POSTs, so any seed authored via the ADK
round-trips through both the bootstrap flow and direct publish.

Public surface:

    Skill / Prompt / McpServer / Agent  — fluent builders
    Publisher                           — POST helper with batch support
    scaffold_skill / scaffold_agent /
    scaffold_prompt / scaffold_mcp      — write a starter YAML to disk
"""

from devai.adk.builders import Agent, McpServer, Prompt, Skill
from devai.adk.publisher import Publisher, PublishResult
from devai.adk.scaffold import (
    scaffold_agent,
    scaffold_mcp_server,
    scaffold_prompt,
    scaffold_skill,
)

__all__ = [
    "Agent",
    "McpServer",
    "Prompt",
    "PublishResult",
    "Publisher",
    "Skill",
    "scaffold_agent",
    "scaffold_mcp_server",
    "scaffold_prompt",
    "scaffold_skill",
]
