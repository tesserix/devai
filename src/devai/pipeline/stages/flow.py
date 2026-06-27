"""Generic control-flow stages — reusable by ANY blueprint with ANY agent.

These are the framework primitives behind the ALM pipeline's multi-story
implementation and quality gates, but nothing here is ALM- or agent-specific.
A blueprint picks the agent, the list to map over, and the gate flags via
``config:`` (or a thin factory passes them in Python). They compose with the
existing ``condition:`` grammar and ``AgentStage`` deriver to express bounded
fix→re-check loops without bespoke per-pipeline code.

  - :class:`ForEachStage` — run an agent once per item in a handover list
    (fan-out / map). Structural fields the first iteration sets (``branch_name``,
    ``pr_number``) are shared with the rest, so e.g. every story commits onto one
    branch / PR. 0–1 items → a single dispatch.
  - :class:`EnforceFlagsStage` — block the run when any configured boolean flag
    in the handover bag is set (a hard gate after a bounded fix loop).
  - :func:`flag_deriver` — build a deriver that turns a string verdict into a
    boolean gate flag, from a ``flag=field:value`` config spec (so a blueprint
    can gate on a verdict with no Python).

Registered generically as the ``for_each`` and ``enforce_flags`` stage keys; the
ALM stages in ``alm.py`` are thin factories over these same classes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from devai.agentruntime import AgentDispatcher, AgentResult, LegacyAgent
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.principal import resolve_principal_run
from devai.pipeline.types import DevAITask, StageResult, TaskState

if TYPE_CHECKING:
    from devai.agentruntime import Agent

logger = logging.getLogger(__name__)

# Same shape as agent_stage.Validator (kept local so flow.py has no dependency on
# the agent-stage module): inspect the handover and raise to fail the stage.
Validator = Callable[..., Awaitable[None]]
Deriver = Callable[[dict[str, Any]], dict[str, Any]]

# Named output validators a config-driven stage (``for_each``) can attach by
# name. Producers register theirs at import (e.g. alm.py registers
# "pull_request"), so the generic factory stays decoupled from where the
# validators live.
NAMED_VALIDATORS: dict[str, Validator] = {}


def register_validator(name: str, fn: Validator) -> None:
    """Make an output validator referenceable from a blueprint's ``config``."""
    NAMED_VALIDATORS[name] = fn


def flag_deriver(spec: str) -> Deriver:
    """Build a deriver from a ``flag=field:value`` spec.

    ``"review_changes_requested=review_decision:changes_requested"`` →
    a callable that sets ``review_changes_requested = True`` when the handover's
    ``review_decision`` equals ``changes_requested`` (case-insensitive). Lets any
    blueprint turn a verdict string into the boolean flag the truthy
    ``condition:`` grammar and :class:`EnforceFlagsStage` branch on.
    """
    flag, _, cond = spec.partition("=")
    field, _, value = cond.partition(":")
    flag, field, value = flag.strip(), field.strip(), value.strip().lower()

    def _derive(patch: dict[str, Any]) -> dict[str, Any]:
        return {flag: str(patch.get(field) or "").lower() == value}

    return _derive


class ForEachStage(PipelineStage):
    """Run ``agent`` once per item in ``task.agent_context[items_key]``.

    The generic fan-out / map primitive. Each iteration runs on the SHARED task,
    so structural fields the first iteration sets (``branch_name``, ``pr_number``)
    are reused by the rest — e.g. multi-story implementation accumulates onto one
    branch / PR while the downstream single-PR pipeline is unchanged. 0–1 items →
    a single dispatch, byte-identical to a plain ``AgentStage``.

    Params:
      agent       — the :class:`Agent` run once per item.
      output_key  — handover namespace for the aggregate result.
      items_key   — handover-bag list to iterate (default ``"stories"``).
      index_key   — field set per iteration so the agent picks its slice (default
                    ``"active_story_index"``); rides ``extra_context`` so the
                    persistent handover bag is never mutated.
      next_state  — task state to advance to.
      validator   — optional output contract run on the AGGREGATE result.
    """

    def __init__(
        self,
        deps: StageDeps,
        *,
        agent: Agent,
        output_key: str,
        name: str = "for_each",
        items_key: str = "stories",
        index_key: str = "active_story_index",
        next_state: TaskState | None = None,
        validator: Validator | None = None,
    ) -> None:
        self.deps = deps
        self._agent = agent
        self._output_key = output_key
        self._name = name
        self._items_key = items_key
        self._index_key = index_key
        self._next_state = next_state
        self._validator = validator
        self._dispatcher = AgentDispatcher(deps)

    def name(self) -> str:
        return self._name

    async def execute(self, task: DevAITask) -> StageResult:
        task.current_stage = task.current_stage or self._name
        # Per-principal config/SCM + trial gate resolved ONCE for the whole map
        # (not per item — that would over-meter the trial).
        config, scm = await resolve_principal_run(self.deps, task, trial_gate=True, stage_name=self._name)

        raw = task.agent_context.get(self._items_key)
        items = raw if isinstance(raw, list) else []
        n = len(items)

        # 0–1 items → one run, exactly like a plain AgentStage.
        if n <= 1:
            result = await self._dispatcher.dispatch(self._agent, task, config=config, scm=scm)
            return await self._finalize(task, result)

        summaries: list[str] = []
        pr_number: int | None = None
        for i in range(n):
            # index rides extra_context (per-dispatch) so the agent picks this
            # item without mutating the shared handover bag.
            result = await self._dispatcher.dispatch(
                self._agent, task, extra_context={self._index_key: i}, config=config, scm=scm
            )
            if result.stub:
                return self._stub()
            branch = result.handover.get("branch_name")
            if isinstance(branch, str) and branch:
                task.branch_name = branch  # first item's branch becomes the shared one
            pr = result.handover.get("pr_number")
            if isinstance(pr, int) and pr > 0 and pr_number is None:
                pr_number = pr
            summaries.append(self._summarize(i, n, items[i], result))
            logger.info("%s: item %d/%d done (branch=%s, pr=%s)", self._name, i + 1, n, task.branch_name, pr_number)

        aggregate = AgentResult(
            ok=True,
            output_key=self._output_key,
            next_state=self._next_state,
            handover={
                "summary": "\n\n".join(summaries),
                # alias the implementation contract readers + the PR validator use
                "implementation_summary": "\n\n".join(summaries),
                "branch_name": task.branch_name,
                "pr_number": pr_number,
                f"{self._items_key}_processed": n,
            },
            message=f"{self._name}: ran {getattr(self._agent, 'name', '?')} over {n} {self._items_key}",
        )
        return await self._finalize(task, aggregate)

    async def _finalize(self, task: DevAITask, result: AgentResult) -> StageResult:
        if result.stub:
            return self._stub()
        result.output_key = self._output_key
        if result.next_state is None:
            result.next_state = self._next_state
        stage_result = result.to_stage_result(task)
        if self._validator:
            await self._validator(self.deps, task, result.handover, stage_name=self._name, output_key=self._output_key)
        return stage_result

    def _stub(self) -> StageResult:
        return StageResult(
            next_state=self._next_state,
            message=f"{self._name} skipped — deps unavailable",
            data={f"{self._output_key}_stub": True},
        )

    @staticmethod
    def _summarize(i: int, n: int, item: Any, result: AgentResult) -> str:
        label = ""
        if isinstance(item, dict):
            label = str(item.get("title") or item.get("name") or "")[:80]
        detail = str(
            result.handover.get("implementation_summary") or result.handover.get("summary") or result.message or ""
        )[:300]
        return f"[{i + 1}/{n}] {label}: {detail}"


class EnforceFlagsStage(PipelineStage):
    """Hard gate: raise (block the run) when any configured boolean flag in the
    handover bag is truthy.

    Generic — the blueprint lists ``flag→reason`` pairs in config. Pair with a
    ``condition:`` so the stage only RUNS when something is unresolved (so
    reaching it IS the block: the executor writes a runbook and labels the issue
    ``devai:needs-human``). This is what makes a verdict actually gate instead of
    being a string nothing reads.
    """

    def __init__(self, deps: StageDeps, *, flags: list[tuple[str, str]], name: str = "enforce_flags") -> None:
        self.deps = deps
        self._flags = flags
        self._name = name

    def name(self) -> str:
        return self._name

    async def execute(self, task: DevAITask) -> StageResult:
        reasons = [reason for flag, reason in self._flags if task.agent_context.get(flag)]
        raise RuntimeError(
            "delivery blocked by a quality gate: "
            + "; ".join(reasons or ["an unresolved gate flag"])
            + ". A bounded fix loop did not resolve it — routing to a human instead of shipping."
        )


# ── Config-driven factories (the generic registry keys) ──────────────────────


def _parse_flags(spec: str) -> list[tuple[str, str]]:
    """Parse ``"flag1:reason1;flag2:reason2"`` → ``[(flag1, reason1), ...]``."""
    out: list[tuple[str, str]] = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        key, _, reason = part.partition(":")
        key = key.strip()
        if key:
            out.append((key, reason.strip() or key))
    return out


def for_each_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Generic ``for_each`` stage built from blueprint config.

    config:
      agent       — dotted path of the agent to run per item (required).
      output_key  — handover namespace (default ``for_each``).
      items_key   — list to iterate (default ``stories``).
      index_key   — per-item index field (default ``active_story_index``).
      validator   — optional name registered via :func:`register_validator`.
    """
    output_key = config.get("output_key") or "for_each"
    dotted = config.get("agent", "")
    agent = LegacyAgent.from_dotted(dotted, name=output_key, output_key=output_key)
    return ForEachStage(
        deps,
        agent=agent,
        output_key=output_key,
        name=config.get("name", "for_each"),
        items_key=config.get("items_key", "stories"),
        index_key=config.get("index_key", "active_story_index"),
        validator=NAMED_VALIDATORS.get(config.get("validator", "")),
    )


def enforce_flags_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Generic ``enforce_flags`` gate built from blueprint config.

    config:
      flags — ``"flag1:reason1;flag2:reason2"`` — the handover flags that block.
    """
    return EnforceFlagsStage(
        deps, flags=_parse_flags(config.get("flags", "")), name=config.get("name", "enforce_flags")
    )


__all__ = [
    "NAMED_VALIDATORS",
    "EnforceFlagsStage",
    "ForEachStage",
    "enforce_flags_stage",
    "flag_deriver",
    "for_each_stage",
    "register_validator",
]
