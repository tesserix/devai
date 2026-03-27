"""Abstract base class for all DevAI agents."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from devai.core.event_bus import EventBus
from devai.core.state import StateManager
from devai.models import AgentResult, PipelineContext

if TYPE_CHECKING:
    from nats.aio.msg import Msg

    from devai.config import Settings
    from devai.core.github_client import GitHubClient

logger = logging.getLogger(__name__)

# Maps agent names to their approval gate names
AGENT_GATE_MAP = {
    "senior_developer": "createPR",
    "staff_reviewer": "review",
    "qa_tester": "testing",
}


class BaseAgent(ABC):
    """Abstract base for all DevAI pipeline agents.

    Each agent subscribes to a NATS subject, processes messages by calling
    the concrete `execute()` method, and publishes results downstream.
    """

    name: str
    subscribe_subject: str
    publish_subject: str

    def __init__(
        self,
        event_bus: EventBus,
        state: StateManager,
        github: GitHubClient,
        config: Settings,
    ) -> None:
        self.event_bus = event_bus
        self.state = state
        self.github = github
        self.config = config
        self.worker_id = f"{self.name}-{uuid.uuid4().hex[:8]}"

    async def start(self) -> None:
        """Subscribe to the agent's NATS subject and begin processing."""
        await self.event_bus.subscribe(
            self.subscribe_subject,
            self._handle_message,
            durable_name=f"devai-{self.name}",
        )
        logger.info("Agent %s started (worker=%s)", self.name, self.worker_id)

    async def _handle_message(self, msg: Msg) -> None:
        """Deserialize context, acquire lock, execute, publish downstream."""
        ctx = PipelineContext.model_validate_json(msg.data)
        run_id = ctx.run_id

        # Acquire distributed lock to prevent duplicate processing
        if not await self.state.acquire_lock(run_id, self.name, self.worker_id):
            logger.warning("Lock not acquired for %s/%s, skipping", run_id, self.name)
            await msg.ack()
            return

        # Check if this agent requires approval before executing
        await self._check_approval_gate(ctx)

        await self.state.set_agent_status(run_id, self.name, "running")
        start_time = time.monotonic()

        # Inject CLAUDE.md governance into context if available
        await self._inject_governance(ctx)

        try:
            result = await self.execute(ctx)
            elapsed = time.monotonic() - start_time
            result.duration_seconds = elapsed

            await self.state.set_agent_status(run_id, self.name, "completed")
            await self.state.store_agent_result(run_id, self.name, result)

            # Enrich context with this agent's output and publish downstream
            ctx.artifacts[self.name] = result.output
            if self.publish_subject:
                await self.event_bus.publish(self.publish_subject, ctx.model_dump_json())

            logger.info(
                "Agent %s completed run %s in %.1fs",
                self.name,
                run_id,
                elapsed,
            )

        except Exception:
            elapsed = time.monotonic() - start_time
            logger.exception("Agent %s failed for run %s after %.1fs", self.name, run_id, elapsed)
            await self.state.set_agent_status(run_id, self.name, "failed", error=str(Exception))
            await self.event_bus.publish(
                "devai.pipeline.errors",
                ctx.model_dump_json(),
            )
            raise
        finally:
            await self.state.release_lock(run_id, self.name, self.worker_id)
            await msg.ack()

    async def _check_approval_gate(self, ctx: PipelineContext) -> None:
        """Check if this agent requires user approval before proceeding.

        If a gate is configured for this agent, it publishes a pending approval
        to Redis and polls until the user approves or rejects from the dashboard.
        """
        gate = AGENT_GATE_MAP.get(self.name)
        if not gate:
            return

        # Check pipeline config for this repo
        raw_config = await self.state.redis.get(f"devai:config:{ctx.repo_full_name}")
        if not raw_config:
            raw_config = await self.state.redis.get("devai:config:default")
        if not raw_config:
            return

        config = json.loads(raw_config)
        if config.get("auto_mode", False):
            return  # Auto mode — skip all gates

        gates = config.get("gates", {})
        if not gates.get(gate, False):
            return  # This specific gate is not enabled

        # Publish approval request
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

        # Poll for approval/rejection (check every 5 seconds, timeout after 1 hour)
        gate_key = f"devai:run:{ctx.run_id}:gate:{gate}"
        for _ in range(720):  # 720 * 5s = 1 hour
            decision = await self.state.redis.get(gate_key)
            if decision == "approved":
                await self.state.redis.delete(gate_key)
                logger.info("Gate '%s' approved for run %s", gate, ctx.run_id)
                return
            if decision == "rejected":
                await self.state.redis.delete(gate_key)
                raise RuntimeError(f"Gate '{gate}' rejected by user for run {ctx.run_id}")
            await asyncio.sleep(5)

        raise RuntimeError(f"Gate '{gate}' timed out waiting for approval on run {ctx.run_id}")

    async def _inject_governance(self, ctx: PipelineContext) -> None:
        """Load CLAUDE.md governance content and inject it into the pipeline context."""
        if "governance" in ctx.artifacts:
            return  # Already loaded
        raw = await self.state.redis.get(f"devai:governance:{ctx.repo_full_name}:claude_md")
        if raw:
            ctx.artifacts["governance"] = raw

    @abstractmethod
    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Agent-specific logic. Must be implemented by each concrete agent."""
        ...
