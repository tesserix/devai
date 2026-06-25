"""Shared helper for stage adapters: the run-correlation label.

The ``AgentAdapter`` base class, the ``_safe_agent`` constructor, and the
per-run ``_agent_config_ctx`` / ``_agent_scm_ctx`` contextvars that used to live
here were retired when the 14 ALM stages moved to the dispatcher-backed
``AgentStage`` (see ``pipeline/stages/agent_stage.py``). Their responsibilities
now live in the Agent SDK:

  - per-principal config/SCM + trial gate → ``pipeline/principal.resolve_principal_run``
  - ALMState build / patch→result mapping → ``agentruntime/agent.py``
  - construction of a legacy ``BaseAgent`` → ``agentruntime/legacy.LegacyAgent``

Only ``run_correlation_label`` remains — it has no dependency on any of that and
is used by several stages + the executor.
"""

from __future__ import annotations


def run_correlation_label(task_id: str) -> str:
    """Label tying every artifact (issues, epic, stories, bugs, PRs) back to
    the fleet run that produced it — e.g. `devai:run:c0ccd293f7`."""
    return f"devai:run:{(task_id or '').removeprefix('devai-')[:10]}"


__all__ = ["run_correlation_label"]
