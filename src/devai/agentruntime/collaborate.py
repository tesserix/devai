"""Collaboration patterns over the Agent dispatcher.

These compose the one ``AgentDispatcher`` into the named multi-agent shapes from
the RecursiveMAS taxonomy — but at the **orchestration layer** (text / structured
handoffs through ``task.agent_context``), which is what fits DevAI's API-based LLM
plane. (RecursiveMAS's own *latent-state* mechanism needs co-located white-box
models on one GPU; only the compositional ideology transfers — see the
``project_sdk_adk`` memory note.)

Each pattern is a plain async function: hand it a dispatcher, the agent(s), and
the task. They build only on ``dispatch`` / ``dispatch_many`` + the handover bag,
so they work with any agent (``LegacyAgent``, ``SpecAgent``, native) and any
backend (inline now, Job later) — and they nest, since an agent can itself call
one of these via ``ctx.spawn``.

    sequential   — agents in order, each seeing prior handovers (the chain)
    mixture      — agents concurrently, then aggregate (specialists + summarizer)
    deliberation — actor ↔ critic loop until accepted (reflect ↔ act)
    distillation — cheap learner first, escalate to the expert on uncertainty
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from devai.agentruntime.agent import AgentResult

if TYPE_CHECKING:
    from devai.agentruntime.agent import Agent
    from devai.agentruntime.dispatch import AgentDispatcher
    from devai.pipeline.types import DevAITask


def _thread(task: DevAITask, result: AgentResult) -> None:
    """Fold an agent's handover back into the bag so the next agent sees it."""
    if result is None:
        return
    if result.output_key:
        task.agent_context[result.output_key] = result.handover
    else:
        task.agent_context.update(result.handover)


async def sequential(
    dispatcher: AgentDispatcher,
    agents: list[Agent],
    task: DevAITask,
    *,
    thread: bool = True,
) -> list[AgentResult]:
    """Run ``agents`` in order; each sees prior agents' handovers in the bag.

    The RecursiveMAS *sequential* pattern. With ``thread=False`` the agents run
    in order but don't see each other's output (a plain batch in a fixed order).
    """
    results: list[AgentResult] = []
    for agent in agents:
        result = await dispatcher.dispatch(agent, task)
        results.append(result)
        if thread:
            _thread(task, result)
    return results


async def mixture(
    dispatcher: AgentDispatcher,
    agents: list[Agent],
    task: DevAITask,
    *,
    aggregate: Callable[[list[AgentResult], DevAITask], Awaitable[AgentResult]] | None = None,
) -> AgentResult:
    """Run ``agents`` concurrently, then combine their handovers.

    The RecursiveMAS *mixture* pattern (domain specialists + a summarizer).
    ``aggregate`` is an async ``(results, task) -> AgentResult``; the default
    merges each agent's handover under its output key. A failed agent is skipped
    in the default merge (``dispatch_many`` turns errors into ``ok=False``).
    """
    results = await dispatcher.dispatch_many(list(agents), task)
    if aggregate is not None:
        return await aggregate(results, task)
    merged: dict = {}
    for result in results:
        if result is not None and not result.error:
            merged[result.output_key or getattr(result, "name", "result")] = result.handover
    return AgentResult(handover=merged, message=f"mixture of {len(results)} agent(s)")


async def deliberation(
    dispatcher: AgentDispatcher,
    actor: Agent,
    critic: Agent,
    task: DevAITask,
    *,
    max_rounds: int = 3,
    accept: Callable[[AgentResult], bool] | None = None,
) -> AgentResult:
    """``actor`` produces, ``critic`` reviews; loop until accepted or out of rounds.

    The RecursiveMAS *deliberation* (reflect ↔ act) pattern — DevAI's review loop
    expressed as a primitive. Each round threads both handovers into the bag, so
    the actor's next attempt can read the critic's feedback. ``accept`` is
    ``(critic_result) -> bool``; default: the critic's handover ``approved`` is
    truthy. The returned actor result carries ``_deliberation_approved``.
    """
    accept = accept or (lambda r: bool(r.handover.get("approved")))
    last: AgentResult | None = None
    for _ in range(max(1, max_rounds)):
        last = await dispatcher.dispatch(actor, task)
        _thread(task, last)
        review = await dispatcher.dispatch(critic, task)
        _thread(task, review)
        if accept(review):
            last.handover["_deliberation_approved"] = True
            return last
    if last is not None:
        last.handover["_deliberation_approved"] = False
    return last if last is not None else AgentResult(ok=False, message="deliberation produced nothing")


async def distillation(
    dispatcher: AgentDispatcher,
    learner: Agent,
    expert: Agent,
    task: DevAITask,
    *,
    escalate: Callable[[AgentResult], bool] | None = None,
) -> AgentResult:
    """Try the cheap ``learner``; escalate to ``expert`` when uncertain.

    The RecursiveMAS *distillation* pattern (small Learner ↔ big Expert), here as
    cheap-first → escalate. ``escalate`` is ``(learner_result) -> bool``; default:
    the learner errored, stubbed, or flagged ``escalate`` in its handover. On
    escalation the learner's handover is threaded into the bag first (so the
    expert sees the attempt) and the expert result carries ``_escalated_from``.
    """
    escalate = escalate or (lambda r: bool(r.error) or r.stub or bool(r.handover.get("escalate")))
    result = await dispatcher.dispatch(learner, task)
    if escalate(result):
        _thread(task, result)
        expert_result = await dispatcher.dispatch(expert, task)
        expert_result.handover.setdefault("_escalated_from", getattr(learner, "name", "learner"))
        return expert_result
    return result


__all__ = ["deliberation", "distillation", "mixture", "sequential"]
