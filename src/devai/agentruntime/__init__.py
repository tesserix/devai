"""Agent runtime — executes a YAML specialization as an autonomous agent.

`AgentRunner` is the keystone that lets a specialization with no
`legacy_python_class` actually *do work*: it runs a bounded tool-calling
loop against the configured `LLMAdapter`, binding only the tools the spec
declares in `allowed_tools` (via `devai.tools.registry`). This is what
makes every YAML role "equipped to do real engineering" and is the unit a
crew invokes once per member.
"""

from devai.agentruntime.runner import AgentRunner, AgentRunResult

__all__ = ["AgentRunResult", "AgentRunner"]
