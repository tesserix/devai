"""Per-principal run resolution — config overlay, SCM client, trial gate.

Lifted verbatim from ``AgentAdapter.execute`` so the new ``AgentStage`` path is
**behavior-identical** to the adapter it replaces. One function, two call sites
(the old adapter and the new stage), so the trial-metering policy lives in one
place instead of being copied.

What it does, for the run's triggering principal (``task.triggered_by``):

  1. Resolve their Settings **overlay** (their own keys/model) when they have an
     LLM connector — else the platform ``config``.
  2. **Trial gate** (only when ``trial_gate=True`` — i.e. running a legacy agent
     that builds its LLM from ``config``): a human with no connector, under
     ``DEVAI_LLM_REQUIRE_USER_CONNECTOR``, whose free-trial budget is gone, gets
     a clear "add your key" ``RuntimeError`` instead of silently riding the
     shared platform keys. With budget left, the turn context is flagged so the
     usage sink meters their spend.
  3. Resolve their own **SCM client** (PAT / GitHub App) when configured.

Returns ``(config, scm)``. Never sets contextvars — the new path constructs the
agent against these values directly (the old adapter's ``_agent_config_ctx`` /
``_agent_scm_ctx`` are only read by ``_safe_agent``, which the new path doesn't
use).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.types import DevAITask
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


def is_human_principal(email: str) -> bool:
    """A real user, not a webhook/system/cron synthetic principal."""
    return bool(email) and "@" in email and not email.startswith(("webhook:", "system:"))


async def resolve_principal_run(
    deps: StageDeps,
    task: DevAITask,
    *,
    trial_gate: bool,
    stage_name: str,
) -> tuple[Settings, SCMClient | None]:
    """Resolve ``(config, scm)`` for ``task``'s principal; apply the trial gate.

    Raises ``RuntimeError`` only in strict mode when a human with no connector
    has exhausted their trial — the same visible failure the adapter produces.
    """
    agent_config = deps.config
    email = str(getattr(task, "triggered_by", "") or "")
    is_human = is_human_principal(email)

    # ── 1. Settings overlay (their keys/model) ─────────────────────────
    has_own = False
    try:
        resolver = getattr(deps, "llm_resolver", None)
        if resolver is not None and is_human:
            if hasattr(resolver, "llm_overlay_for_email"):
                overlay, has_own = await resolver.llm_overlay_for_email(email)
            else:  # older resolver surface (tests/doubles)
                overlay = await resolver.settings_for_email(email)
                has_own = overlay is not None and overlay is not deps.config
            if has_own and overlay is not None:
                agent_config = overlay
    except Exception:  # noqa: BLE001
        logger.debug("stage %s: per-user config resolution failed", stage_name, exc_info=True)

    # ── 2. Trial gate + billing attribution ────────────────────────────
    if (
        trial_gate
        and is_human
        and not has_own
        and getattr(deps, "llm_resolver", None) is not None
        and bool(getattr(deps.config, "llm_require_user_connector", False))
    ):
        from devai.settings.trial import get_trial_meter

        budget = int(getattr(deps.config, "llm_trial_token_budget", 0) or 0)
        meter = get_trial_meter(deps.config)
        if budget <= 0 or await meter.exhausted(email):
            raise RuntimeError(
                f"stage {stage_name}: no LLM available for {email} — their free "
                "trial allowance is used up and no LLM connector is configured. "
                "Add an API key in Settings → LLM Provider to continue."
            )
        # Trial mode: flag the turn context so every usage envelope the agent's
        # provider emits is metered against this user's budget.
        from devai.services.agent_turns import update_turn_context

        update_turn_context(triggered_by=email, trial="1")
    else:
        from devai.services.agent_turns import update_turn_context

        update_turn_context(triggered_by=email)

    # ── 3. Per-principal SCM client (their PAT / GitHub App) ────────────
    agent_scm = deps.scm
    try:
        if is_human and getattr(deps, "scm_resolver", None) is not None:
            agent_scm = await deps.scm_for_principal(email) or deps.scm
    except Exception:  # noqa: BLE001
        logger.debug("stage %s: per-user SCM resolution failed", stage_name, exc_info=True)

    return agent_config, agent_scm


__all__ = ["is_human_principal", "resolve_principal_run"]
