"""Abstract base class for all DevAI agents.

Supports two execution modes:
1. LangGraph node: Called via run(state) from the graph orchestrator
2. NATS subscriber: Called via start() for legacy event-driven execution

All agents get LangSmith tracing when enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from devai.graph.a2a import A2ABus
from devai.graph.state import ALMState
from devai.services.tracing import is_tracing_enabled

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


def _delivery_count(msg: Any) -> int:
    """Best-effort JetStream delivery count for a message, or 0 if unknown.

    NATS exposes it at ``msg.metadata.num_delivered``; tolerate doubles that
    don't carry metadata.
    """
    try:
        meta = getattr(msg, "metadata", None)
        return int(getattr(meta, "num_delivered", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


# Maps agent names to their approval gate names
AGENT_GATE_MAP = {
    "senior_developer": "createPR",
    "staff_reviewer": "review",
    "qa_tester": "testing",
    "release_manager": "deploy",
}


class BaseAgent(ABC):
    """Abstract base for all DevAI pipeline agents.

    Each agent can operate as a LangGraph node (via run()) or as a
    NATS subscriber (via start()) for backward compatibility.
    """

    name: str

    # NATS subjects (used only in legacy mode)
    subscribe_subject: str = ""
    publish_subject: str = ""

    def __init__(
        self,
        scm: SCMClient,
        state_manager: StateManager,
        config: Settings,
        event_bus: EventBus | EventBusAdapter | None = None,
    ) -> None:
        # Self-heal the SCM client at construction time. The webhook
        # path always passes a proper SCMClient subclass (created via
        # the multi-SCM factory), but other entry points (the dashboard
        # used to be one) sometimes passed the legacy
        # ``core.github_client.GitHubClient``. That class is missing
        # newer abstract-base helpers like ``create_issue_idempotent``
        # and ``list_issues``, which would crash agents at runtime with
        # an AttributeError. We detect that here and silently swap in
        # the proper client so every agent gets a uniform interface.
        self.scm = self._heal_scm_client(scm, config)
        # Backward-compatible alias (agents may still reference self.github)
        self.github = self.scm
        self.state = state_manager
        self.config = config
        self.event_bus = event_bus
        self.worker_id = f"{self.name}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _heal_scm_client(scm: Any, config: Any) -> Any:
        """If the injected SCM client is the legacy standalone class,
        swap in the proper SCMClient subclass from the factory so all
        agents get the full interface (dedup helpers, list_issues, etc.).

        Defensive — never raises. If the swap fails for any reason, the
        original client is returned and the agent falls back to whatever
        interface the original client exposes (the agent-level
        hasattr-checks are the second line of defense).
        """
        try:
            from devai.scm.base import SCMClient as _SCMClient

            if isinstance(scm, _SCMClient):
                return scm

            from devai.scm import create_scm_client as _factory

            healed = _factory(config)
            logger.warning(
                "BaseAgent._heal_scm_client: replaced legacy %s with %s "
                "from create_scm_client() so dedup helpers are available",
                type(scm).__name__,
                type(healed).__name__,
            )
            return healed
        except Exception as e:
            logger.warning(
                "BaseAgent._heal_scm_client: could not heal %s (%s) — keeping injected client",
                type(scm).__name__,
                e,
            )
            return scm

    # --- LangGraph Node Execution ---

    async def run(self, state: ALMState) -> dict[str, Any]:
        """Execute the agent as a LangGraph node.

        Reads from ALMState, performs work, returns partial state updates.
        Includes guardrails, A2A messaging, and LangSmith tracing.
        """
        # Run pre-execution guardrail checks
        await self._guardrail_pre_check(state)

        # Pull caller identity off the state (stamped at the boundary
        # by webhook / chat / dashboard handlers). Falls back to empty
        # strings — the bus tolerates that and just omits the fields
        # on outbound messages.
        principal = state.get("principal") or {}
        triggered_by = (
            state.get("trigger_actor")
            or (principal.get("email") if isinstance(principal, dict) else "")
            or ""
        )
        trace_id = state.get("trace_id", "") or ""

        # Create A2A bus scoped to this agent, with identity threaded
        # through so every outbound message carries the originating user
        # and trace id without each agent having to remember to set them.
        a2a = A2ABus(
            self.name,
            state.get("a2a_messages", []),
            triggered_by=triggered_by,
            trace_id=trace_id,
        )

        if triggered_by or trace_id:
            logger.info(
                "agent.run start: name=%s run_id=%s triggered_by=%s trace_id=%s",
                self.name,
                state.get("run_id", ""),
                triggered_by or "-",
                trace_id or "-",
            )

        # Execute the agent with tracing
        _run_fn = self._execute_graph
        if is_tracing_enabled():
            try:
                from langsmith import traceable

                @traceable(name=f"agent.{self.name}", run_type="chain")
                async def traced_run(s: ALMState, bus: A2ABus) -> dict[str, Any]:
                    return await self._execute_graph(s, bus)

                _run_fn = traced_run
            except ImportError:
                pass

        result = await _run_fn(state, a2a)

        # Run post-execution guardrail checks on output
        result = self._guardrail_post_check(result)

        # Merge A2A messages: start with existing, add agent's outbox,
        # and include any messages the agent may have added to the result directly
        existing_messages = list(state.get("a2a_messages", []))
        existing_ids = {m.get("id") for m in existing_messages if isinstance(m, dict)}

        direct_messages = result.get("a2a_messages", [])
        if isinstance(direct_messages, list):
            for msg in direct_messages:
                if isinstance(msg, dict) and msg.get("id") not in existing_ids:
                    existing_messages.append(msg)
                    existing_ids.add(msg.get("id"))

        # Add outbox messages (sent via the a2a bus parameter)
        for msg in a2a.collect_outbox():
            if msg.get("id") not in existing_ids:
                existing_messages.append(msg)
                existing_ids.add(msg.get("id"))

        result["a2a_messages"] = existing_messages

        return result

    @abstractmethod
    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Agent-specific logic for LangGraph execution.

        Args:
            state: Current ALM pipeline state.
            a2a: A2A message bus for inter-agent communication.

        Returns:
            Partial state update dict to merge into ALMState.
        """
        ...

    # --- NATS Legacy Execution ---

    async def start(self) -> None:
        """Subscribe to the agent's NATS subject and begin processing (legacy mode).

        Works against both the new EventBusAdapter and the legacy
        EventBus wrapper — both expose `subscribe(subject, handler,
        durable_name=...)`. The handler signature is duck-typed: both
        envelopes carry `.data` (bytes) and an async `.ack()`.
        """
        if not self.event_bus:
            raise RuntimeError(f"Agent {self.name}: event_bus required for NATS mode")
        if not self.subscribe_subject:
            raise RuntimeError(
                f"Agent {self.name}: subscribe_subject is empty — agent has no inbound subject"
            )
        await self.event_bus.subscribe(
            self.subscribe_subject,
            self._handle_message,
            durable_name=f"devai-{self.name}",
        )
        logger.info(
            "Agent %s started (worker=%s, mode=nats, subject=%s)",
            self.name,
            self.worker_id,
            self.subscribe_subject,
        )

    async def _handle_message(self, msg: Any) -> None:
        """Deserialize context, acquire lock, execute, publish downstream (legacy)."""
        from devai.models import PipelineContext

        ctx = PipelineContext.model_validate_json(msg.data)
        run_id = ctx.run_id

        if not await self.state.acquire_lock(run_id, self.name, self.worker_id):
            logger.warning("Lock not acquired for %s/%s, skipping", run_id, self.name)
            await msg.ack()
            return

        await self._check_approval_gate(ctx)
        await self.state.set_agent_status(run_id, self.name, "running")
        start_time = time.monotonic()

        succeeded = False
        try:
            # Convert PipelineContext to ALMState for unified execution
            alm_state: ALMState = {
                "run_id": ctx.run_id,
                "repo_full_name": ctx.repo_full_name,
                "requirements": ctx.requirements,
                "stage": ctx.stage.value,
                "branch_name": ctx.branch_name,
                "pr_number": ctx.pr_number,
                "review_iteration": ctx.review_iteration,
                "a2a_messages": [],
            }
            # Copy artifacts
            for key, val in ctx.artifacts.items():
                if key in ALMState.__annotations__:
                    alm_state[key] = val  # type: ignore[literal-required]

            a2a = A2ABus(self.name)
            result_state = await self._execute_graph(alm_state, a2a)

            elapsed = time.monotonic() - start_time
            await self.state.set_agent_status(run_id, self.name, "completed")

            # Enrich context and publish downstream
            ctx.artifacts[self.name] = result_state
            if self.publish_subject and self.event_bus:
                await self.event_bus.publish(self.publish_subject, ctx.model_dump_json())

            logger.info("Agent %s completed run %s in %.1fs", self.name, run_id, elapsed)
            succeeded = True

        except Exception:
            elapsed = time.monotonic() - start_time
            logger.exception("Agent %s failed for run %s after %.1fs", self.name, run_id, elapsed)
            await self.state.set_agent_status(run_id, self.name, "failed")
            if self.event_bus:
                await self.event_bus.publish("devai.pipeline.errors", ctx.model_dump_json())
            raise
        finally:
            await self.state.release_lock(run_id, self.name, self.worker_id)
            # Acknowledge ONLY on success. On failure, negative-ack so JetStream
            # redelivers (up to max_deliver); past the delivery ceiling, term()
            # the message so it's not redelivered forever (poison-pill). Acking
            # a failed message here would silently drop the work and defeat the
            # WORK_QUEUE durability + max_deliver design.
            if succeeded:
                await msg.ack()
            else:
                await self._nak_or_term(msg)

    async def _nak_or_term(self, msg: Any) -> None:
        """Negative-ack a failed message for redelivery, or terminate if the
        delivery ceiling is reached (poison-pill). Tolerates message objects
        that don't expose delivery metadata or nak/term (degrades to nak,
        then to a best-effort no-op) so non-JetStream/test doubles don't break.
        """
        max_deliver = getattr(self.config, "nats_max_deliver", 3)
        delivered = _delivery_count(msg)
        try:
            if delivered and delivered >= max_deliver and hasattr(msg, "term"):
                logger.error(
                    "Agent %s: message hit max_deliver=%s — terminating (poison)",
                    self.name,
                    max_deliver,
                )
                await msg.term()
            elif hasattr(msg, "nak"):
                await msg.nak()
            elif hasattr(msg, "ack"):
                # No nak available (non-JetStream double): leave it for ack_wait
                # redelivery by NOT acking. Nothing to do.
                return
        except Exception:  # noqa: BLE001
            logger.exception("Agent %s: nak/term failed", self.name)

    async def _check_approval_gate(self, ctx: Any) -> None:  # noqa: C901
        """Check if this agent requires user approval before proceeding."""
        gate = AGENT_GATE_MAP.get(self.name)
        if not gate:
            return

        raw_config = await self.state.redis.get(f"devai:config:{ctx.repo_full_name}")
        if not raw_config:
            raw_config = await self.state.redis.get("devai:config:default")
        if not raw_config:
            return

        config = json.loads(raw_config)
        if config.get("auto_mode", False):
            return

        gates = config.get("gates", {})
        if not gates.get(gate, False):
            return

        approval = json.dumps(
            {
                "run_id": ctx.run_id,
                "gate": gate,
                "agent": self.name,
                "repo": ctx.repo_full_name,
                "timestamp": time.time(),
            }
        )
        await self.state.redis.rpush(f"devai:run:{ctx.run_id}:approvals", approval)
        await self.state.set_agent_status(ctx.run_id, self.name, "waiting_approval")

        logger.info("Agent %s waiting for approval gate '%s' on run %s", self.name, gate, ctx.run_id)

        gate_key = f"devai:run:{ctx.run_id}:gate:{gate}"
        for _ in range(720):
            decision = await self.state.redis.get(gate_key)
            if decision == "approved":
                await self.state.redis.delete(gate_key)
                return
            if decision == "rejected":
                await self.state.redis.delete(gate_key)
                raise RuntimeError(f"Gate '{gate}' rejected by user for run {ctx.run_id}")
            await asyncio.sleep(5)

        raise RuntimeError(f"Gate '{gate}' timed out on run {ctx.run_id}")

    # --- Guardrail Integration ---

    async def _guardrail_pre_check(self, state: ALMState) -> None:
        """Run guardrail checks before agent execution."""
        try:
            from devai.services.guardrails import Guardrails

            guardrails = Guardrails(self.config, self.state.redis)
            warnings = await guardrails.pre_agent_check(self.name, dict(state))
            for w in warnings:
                logger.warning("Guardrail: %s", w)
        except Exception as e:
            # Guardrails should never block execution due to their own errors
            logger.debug("Guardrail pre-check skipped: %s", e)

    def _guardrail_post_check(self, result: dict[str, Any]) -> dict[str, Any]:
        """Filter agent output through guardrails."""
        try:
            from devai.services.guardrails import OutputFilter

            output_filter = OutputFilter()

            # Filter any text fields that might be posted to SCM
            for key in (
                "implementation_summary",
                "review_summary",
                "security_summary",
                "test_summary",
                "supervisor_plan_raw",
                "orchestrator_decision_raw",
            ):
                if key in result and isinstance(result[key], str):
                    result[key] = output_filter.filter_for_scm(result[key])
        except Exception as e:
            logger.debug("Guardrail post-check skipped: %s", e)

        return result
