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
from devai.services.tracing import is_tracing_enabled, traceable_if_enabled

if TYPE_CHECKING:
    from nats.aio.msg import Msg

    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.github_client import GitHubClient
    from devai.core.state import StateManager

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
        github: "GitHubClient",
        state_manager: "StateManager",
        config: "Settings",
        event_bus: "EventBus | None" = None,
    ) -> None:
        self.github = github
        self.state = state_manager
        self.config = config
        self.event_bus = event_bus
        self.worker_id = f"{self.name}-{uuid.uuid4().hex[:8]}"

    # --- LangGraph Node Execution ---

    async def run(self, state: ALMState) -> dict[str, Any]:
        """Execute the agent as a LangGraph node.

        Reads from ALMState, performs work, returns partial state updates.
        Includes A2A messaging support and LangSmith tracing.
        """
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

        # Merge A2A outbox into state
        existing_messages = list(state.get("a2a_messages", []))
        existing_messages.extend(a2a.collect_outbox())
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

    async def _handle_message(self, msg: "Msg") -> None:
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

    async def _check_approval_gate(self, ctx: Any) -> None:
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

        approval = json.dumps({
            "run_id": ctx.run_id,
            "gate": gate,
            "agent": self.name,
            "repo": ctx.repo_full_name,
            "timestamp": time.time(),
        })
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
