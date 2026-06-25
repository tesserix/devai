"""Agent runtime — the unified SDK + ADK seam for running DevAI agents.

This package is DevAI's agent runtime: one contract for what an agent *is*, and
one dispatcher for how an agent *runs*. It supersedes the three reflection-based
agent-resolution sites and the two divergent YAML runners that grew up
independently (graph orchestrator, run_specialization stage, Job entrypoint).

Two layers:

**SDK — what an agent is** (author-time contract)
    - ``Agent``          — the one protocol: ``run(ctx: RunContext) -> AgentResult``
    - ``RunContext``     — the wired capabilities + task handed to an agent
    - ``AgentResult``    — the typed result (``to_stage_result()`` bridges to the pipeline)
    - ``LegacyAgent``    — adapts an existing ``BaseAgent`` to the protocol
    - ``SpecAgent``      — runs a YAML ``Specialization`` (the missing ``invoke()``)
    - ``AgentRunner``    — the canonical tool-calling loop a YAML agent executes

**ADK — how an agent runs** (run-time dispatch)
    - ``AgentDispatcher`` — resolves per-principal LLM/SCM/config, runs via a backend
    - ``ExecutionBackend``/``InlineBackend`` — pluggable execution (Job backend later)

Recursion (ROMA / RecursiveMAS-style decomposition) falls out for free: every
``RunContext`` carries the dispatcher, so an agent can ``await ctx.spawn(sub_agent)``.
"""

from devai.agentruntime.agent import (
    DEFAULT_SURFACE_KEYS,
    Agent,
    AgentResult,
    RunContext,
    alm_patch_to_result,
    build_alm_state,
)
from devai.agentruntime.collaborate import deliberation, distillation, mixture, sequential
from devai.agentruntime.dispatch import AgentDispatcher, ExecutionBackend, InlineBackend
from devai.agentruntime.legacy import LegacyAgent
from devai.agentruntime.runner import AgentRunner, AgentRunResult
from devai.agentruntime.spec_agent import SpecAgent

__all__ = [
    "DEFAULT_SURFACE_KEYS",
    "Agent",
    "AgentDispatcher",
    "AgentResult",
    "AgentRunResult",
    "AgentRunner",
    "ExecutionBackend",
    "InlineBackend",
    "LegacyAgent",
    "RunContext",
    "SpecAgent",
    "alm_patch_to_result",
    "build_alm_state",
    "deliberation",
    "distillation",
    "mixture",
    "sequential",
]
