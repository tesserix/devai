"""Stage ABI and dependency injection bundle.

Every stage in the registry implements `PipelineStage`. The blueprint
executor doesn't know or care what a stage actually does — it only sees
`name()`, `execute()`, `rollback()`, and the StageResult that comes back.

StageDeps is the bundle of shared services every stage needs (SCM client,
state manager, config, A2A bus, …). It's passed to stage factories at
registration time and held by closure; stages never reach into globals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from devai.pipeline.types import DevAITask, StageResult

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.adapters.llm.base import LLMAdapter
    from devai.adapters.memory.base import MemoryAdapter
    from devai.adapters.secrets.base import SecretsAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.graph.a2a import A2ABus
    from devai.scm.base import SCMClient


@dataclass(frozen=True, slots=True)
class StageDeps:
    """Services that stage factories close over.

    Frozen — these are wired once at startup and don't change per task.
    Per-task state lives on the DevAITask itself.

    Any of these can be None for stages that don't need them (e.g. a
    pure analysis stage doesn't need the SCM client). Stages MUST tolerate
    None gracefully so unit tests can build a minimal StageDeps without
    standing up Redis / Postgres / GitHub.

    Adapter fields (memory, future: vector_store, event_bus, secrets, ...)
    let stages talk to integrations through stable interfaces; the
    factory layer picks the concrete backend via env-var config.
    """

    config: Settings
    scm: SCMClient | None = None
    state_manager: StateManager | None = None
    # Legacy raw EventBus reference — kept for the handful of stages
    # that still call `core/pipeline.py:PipelineOrchestrator`. New code
    # should publish via `event_bus_adapter` instead. The two point at
    # the same NATS connection but the adapter exposes the stable
    # `EventBusAdapter` interface (noop fallback, headers, request/reply).
    event_bus: EventBus | None = None
    a2a_bus: A2ABus | None = None

    # ── Adapters ─────────────────────────────────────────────────────
    # Constructed via devai.adapters.* factories. `None` means the
    # corresponding integration is disabled; stages must tolerate it.
    memory: MemoryAdapter | None = None
    llm: LLMAdapter | None = None
    event_bus_adapter: EventBusAdapter | None = None
    # Secrets backend (gcp_sm/env/noop) + the SettingsService. Together they
    # let a stage resolve a per-user/per-tenant settings overlay at execution
    # time from the principal in task.agent_context — so a run uses the
    # triggering user's own LLM/SCM/MCP credentials. Secret VALUES are never
    # persisted in task state; only resolved transiently here. Both are None
    # when the Settings capability is disabled, and stages must tolerate that.
    secrets: SecretsAdapter | None = None
    settings_service: Any = None
    # Per-principal LLM resolution (settings.llm_resolver.PrincipalLLMResolver).
    # Stages call `await deps.llm_for_principal(email)` — falls back to
    # `deps.llm` when the user configured nothing. Untyped to avoid an
    # import cycle; None when the Settings capability is disabled.
    llm_resolver: Any = None

    # Pluggable LLM providers — None means "stages use their hardcoded
    # default provider" (the way existing agents do today). Once we move
    # to specializations-as-YAML, the spec declares its provider and we
    # plumb it through here.
    extra: dict[str, Any] | None = None

    async def llm_for_principal(self, email: str) -> LLMAdapter | None:
        """The triggering user's own LLM adapter (their Settings connectors),
        falling back to the platform default. Stages that talk to an LLM
        should prefer this over reading ``deps.llm`` directly.

        With ``DEVAI_LLM_REQUIRE_USER_CONNECTOR=true``, a human principal
        with no LLM connector gets ``None`` (→ a clear stub telling them to
        configure Settings) instead of silently riding the shared platform
        key. Synthetic principals (webhook/system, non-email triggered_by)
        always keep the platform adapter so automation never breaks.
        """
        if self.llm_resolver is None:
            return self.llm

        is_human = bool(email) and "@" in email and not email.startswith(("webhook:", "system:"))

        if is_human:
            adapter = await self.llm_resolver.resolve_for_email(email)
            if adapter is not None:
                return adapter
            if bool(getattr(self.config, "llm_require_user_connector", False)):
                # Strict mode — but a trial budget lets new users test on the
                # platform chain, metered per token. At exhaustion the trial
                # adapter answers with a clear "add your own key" message and
                # the shared keys stay revoked for that user permanently.
                budget = int(getattr(self.config, "llm_trial_token_budget", 0) or 0)
                if budget > 0 and self.llm is not None:
                    from devai.settings.trial import TrialLLMAdapter, get_trial_meter

                    return TrialLLMAdapter(self.llm, get_trial_meter(self.config), email)
                return None
            return self.llm

        # System/webhook/cron runs (SRE, scheduled scans, automation). When a
        # service principal is configured, these resolve ITS Settings
        # connector through the same overlay/gateway path as a human user —
        # so automation also runs on a tenant connector, not the raw shared
        # keys. Falls back to the platform chain only when unset/unconfigured.
        service_email = getattr(self.config, "llm_system_principal", "") or ""
        if service_email:
            adapter = await self.llm_resolver.resolve_for_email(service_email)
            if adapter is not None:
                return adapter
        return self.llm

    async def role_llm_for_principal(self, email: str, role: str) -> LLMAdapter | None:
        """Role-priced chain on the TRIGGERING USER's credentials.

        The one policy every adapter-path LLM call should follow:
          1. user has their own LLM connector → role chain built FROM THEIR
             overlay (their keys, role-routed model);
          2. no connector + strict mode (``DEVAI_LLM_REQUIRE_USER_CONNECTOR``)
             → the platform role chain METERED against their trial budget,
             or ``None`` once the budget is exhausted/disabled (callers must
             degrade gracefully — skip the nicety or fail with a clear
             "add your key in Settings" message);
          3. no connector, permissive mode → platform role chain;
          4. system/webhook principals → platform role chain, never metered.
        """
        from devai.adapters.llm import role_llm_or

        is_human = bool(email) and "@" in email and not email.startswith(("webhook:", "system:"))
        settings, has_own = self.config, False
        if self.llm_resolver is not None and is_human:
            try:
                settings, has_own = await self.llm_resolver.llm_overlay_for_email(email)
            except Exception:  # noqa: BLE001
                settings, has_own = self.config, False

        chain = role_llm_or(settings, role, self.llm)
        if has_own or not is_human or self.llm_resolver is None:
            return chain
        if bool(getattr(self.config, "llm_require_user_connector", False)):
            budget = int(getattr(self.config, "llm_trial_token_budget", 0) or 0)
            if budget > 0 and chain is not None:
                from devai.settings.trial import TrialLLMAdapter, get_trial_meter

                return TrialLLMAdapter(chain, get_trial_meter(self.config), email)
            return None
        return chain


class PipelineStage(ABC):
    """The contract every stage implements.

    The blueprint executor calls `execute()` and consumes the StageResult.
    On failure, depending on the blueprint's `on_failure` policy, it may
    call `rollback()` to let the stage clean up any partial state it created.
    """

    @abstractmethod
    def name(self) -> str:
        """Stable identifier — matches the `stage:` key in the YAML."""

    @abstractmethod
    async def execute(self, task: DevAITask) -> StageResult:
        """Run the stage. Must be idempotent on retry.

        Implementations should:
        - Read context they need from `task.agent_context`.
        - Write outputs to StageResult.data (NOT directly to task.agent_context).
          The executor merges StageResult.data → task.agent_context after
          a successful return.
        - Raise to signal a hard failure; the executor maps the exception
          to a StageEvent(phase=FAILED) and applies the blueprint's
          on_failure policy.
        """

    async def rollback(self, task: DevAITask) -> None:
        """Compensating action when on_failure=rollback.

        Default is a no-op — most stages have nothing to undo. Stages that
        create external resources (issues, branches, sandbox pods) should
        override.
        """
        return None


# Stage factories are how stages get instantiated. The blueprint YAML
# declares `stage: <key>` and `config:` map; the registry looks up the
# factory by key and calls factory(deps, config) → PipelineStage.
StageFactory = Callable[[StageDeps, dict[str, str]], PipelineStage]


class StageRunner(Protocol):
    """Minimum interface the BlueprintExecutor requires from its host.

    Exists so unit tests can hand the executor a fake event sink without
    standing up the full Pipeline.
    """

    def emit_event(self, task: DevAITask, event: Any) -> None: ...
