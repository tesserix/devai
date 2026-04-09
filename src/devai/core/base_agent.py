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
    from nats.aio.msg import Msg

    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)

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
        event_bus: EventBus | None = None,
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
                "BaseAgent._heal_scm_client: could not heal %s (%s) — "
                "keeping injected client",
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

        # Create A2A bus scoped to this agent
        a2a = A2ABus(self.name, state.get("a2a_messages", []))

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
        """Subscribe to the agent's NATS subject and begin processing (legacy mode)."""
        if not self.event_bus:
            raise RuntimeError(f"Agent {self.name}: event_bus required for NATS mode")
        await self.event_bus.subscribe(
            self.subscribe_subject,
            self._handle_message,
            durable_name=f"devai-{self.name}",
        )
        logger.info("Agent %s started (worker=%s, mode=nats)", self.name, self.worker_id)

    async def _handle_message(self, msg: Msg) -> None:
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

        except Exception:
            elapsed = time.monotonic() - start_time
            logger.exception("Agent %s failed for run %s after %.1fs", self.name, run_id, elapsed)
            await self.state.set_agent_status(run_id, self.name, "failed")
            if self.event_bus:
                await self.event_bus.publish("devai.pipeline.errors", ctx.model_dump_json())
            raise
        finally:
            await self.state.release_lock(run_id, self.name, self.worker_id)
            await msg.ack()

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
