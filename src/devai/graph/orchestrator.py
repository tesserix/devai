"""LangGraph ALM Pipeline Orchestrator.

Full Application Lifecycle Management pipeline with Supervisor/Orchestrator hierarchy:

    User Request
      |
    supervisor ──────── (plans architecture, creates delegation plan)
      |
    orchestrator_pre ── (validates plan, sets up execution workflow)
      |
    ingest_documents ── (extracts requirements from PDFs, URLs, specs)
      |
    detect_tech_stack ── (auto-detects language, framework, deployment target)
      |
    analyze_requirements
      |
    create_epic
      |
    create_stories
      |
    create_plan
      |
    implement_code
      |
    db_engineering
      |
    review_code ──────→ orchestrator_review → (changes_requested) → implement_code
      | (approved)
    security_scan ────→ orchestrator_security → (block) → implement_code
      | (pass)
    monitor_build
      |
    run_tests ────────→ orchestrator_tests → (failed) → implement_code
      | (passed)
    provision_infra
      |
    deploy_release
      |
    orchestrator_post ── (final status report)
      |
    END

Features:
- Supervisor Agent plans architecture before execution begins
- Orchestrator Agent makes dynamic routing decisions at quality gates
- State checkpointing at each stage boundary
- Agent memory injection for cross-run learning
- A2A message persistence to Redis
- Per-node timeout protection (15 min)
- LangSmith tracing on every node
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, StateGraph

from devai.graph.state import ALMState
from devai.services.resilience import StateCheckpoint, with_timeout
from devai.services.tracing import traceable_if_enabled

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.core.state import StateManager
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 3
MAX_TEST_FIX_ITERATIONS = 2
NODE_TIMEOUT = 900  # 15 minutes per agent


class ALMOrchestrator:
    """LangGraph-based ALM pipeline orchestrator with Supervisor/Orchestrator hierarchy."""

    def __init__(
        self,
        scm: SCMClient,
        state_manager: StateManager,
        config: Settings,
    ) -> None:
        self.scm = scm
        self.state_manager = state_manager
        self.config = config
        self._checkpoint = StateCheckpoint(state_manager.redis)
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        """Build and compile the full ALM StateGraph with Supervisor/Orchestrator."""
        graph = StateGraph(ALMState)

        # --- Register all nodes ---
        # Supervisor & Orchestrator (coordination layer)
        graph.add_node("supervisor", self._node_supervisor)
        graph.add_node("orchestrator_pre", self._node_orchestrator_pre)
        graph.add_node("orchestrator_review", self._node_orchestrator_review)
        graph.add_node("orchestrator_security", self._node_orchestrator_security)
        graph.add_node("orchestrator_tests", self._node_orchestrator_tests)
        graph.add_node("orchestrator_post", self._node_orchestrator_post)

        # Specialist agents (execution layer)
        graph.add_node("ingest_documents", self._node_ingest_documents)
        graph.add_node("detect_tech_stack", self._node_detect_tech_stack)
        graph.add_node("analyze_requirements", self._node_analyze_requirements)
        graph.add_node("create_epic", self._node_create_epic)
        graph.add_node("create_stories", self._node_create_stories)
        graph.add_node("create_plan", self._node_create_plan)
        graph.add_node("implement_code", self._node_implement_code)
        graph.add_node("db_engineering", self._node_db_engineering)
        graph.add_node("review_code", self._node_review_code)
        graph.add_node("security_scan", self._node_security_scan)
        graph.add_node("monitor_build", self._node_monitor_build)
        graph.add_node("run_tests", self._node_run_tests)
        graph.add_node("provision_infra", self._node_provision_infra)
        graph.add_node("deploy_release", self._node_deploy_release)

        # --- Define edges ---

        # Entry: Supervisor plans first
        graph.set_entry_point("supervisor")

        # Supervisor -> Orchestrator validates and starts execution
        graph.add_edge("supervisor", "orchestrator_pre")

        # Orchestrator kicks off the analysis phase
        graph.add_edge("orchestrator_pre", "ingest_documents")

        # Linear flow: docs -> tech -> requirements -> epic -> stories -> plan -> code
        graph.add_edge("ingest_documents", "detect_tech_stack")
        graph.add_edge("detect_tech_stack", "analyze_requirements")
        graph.add_edge("analyze_requirements", "create_epic")
        graph.add_edge("create_epic", "create_stories")
        graph.add_edge("create_stories", "create_plan")
        graph.add_edge("create_plan", "implement_code")
        graph.add_edge("implement_code", "db_engineering")
        graph.add_edge("db_engineering", "review_code")

        # After review: Orchestrator makes the routing decision
        graph.add_edge("review_code", "orchestrator_review")
        graph.add_conditional_edges(
            "orchestrator_review",
            self._route_after_review,
            {
                "approved": "security_scan",
                "changes_requested": "implement_code",
                "max_iterations": "orchestrator_post",
            },
        )

        # After security: Orchestrator makes the routing decision
        graph.add_edge("security_scan", "orchestrator_security")
        graph.add_conditional_edges(
            "orchestrator_security",
            self._route_after_security,
            {
                "pass": "monitor_build",
                "pass_with_warnings": "monitor_build",
                "block": "implement_code",
                "max_blocks": "orchestrator_post",
            },
        )

        graph.add_edge("monitor_build", "run_tests")

        # After tests: Orchestrator makes the routing decision
        graph.add_edge("run_tests", "orchestrator_tests")
        graph.add_conditional_edges(
            "orchestrator_tests",
            self._route_after_tests,
            {
                "passed": "provision_infra",
                "failed": "implement_code",
                "max_failures": "orchestrator_post",
            },
        )

        graph.add_edge("provision_infra", "deploy_release")

        # After deploy: Orchestrator produces final report
        graph.add_edge("deploy_release", "orchestrator_post")
        graph.add_edge("orchestrator_post", END)

        return graph.compile()

    # --- Routing Functions (Orchestrator-powered) ---

    def _route_after_review(self, state: ALMState) -> str:
        """Orchestrator-informed routing after code review."""
        decision = state.get("review_decision", "changes_requested")
        iteration = state.get("review_iteration", 0)

        # Check orchestrator's recommendation
        routing = state.get("orchestrator_routing", {})
        if routing.get("decision") == "escalate":
            logger.warning("Orchestrator escalated at review stage")
            return "max_iterations"

        if decision == "approved":
            return "approved"
        if iteration >= MAX_REVIEW_ITERATIONS:
            logger.warning("Max review iterations (%d) reached", MAX_REVIEW_ITERATIONS)
            return "max_iterations"
        return "changes_requested"

    def _route_after_security(self, state: ALMState) -> str:
        """Orchestrator-informed routing after security scan."""
        decision = state.get("security_decision", "pass")
        iteration = state.get("review_iteration", 0)

        routing = state.get("orchestrator_routing", {})
        if routing.get("decision") == "escalate":
            return "max_blocks"

        if decision in ("pass", "pass_with_warnings"):
            return decision
        if iteration >= MAX_REVIEW_ITERATIONS + 1:
            return "max_blocks"
        return "block"

    def _route_after_tests(self, state: ALMState) -> str:
        """Orchestrator-informed routing after test execution."""
        test_failed = state.get("test_failed", 0)
        iteration = state.get("review_iteration", 0)

        routing = state.get("orchestrator_routing", {})
        if routing.get("decision") == "escalate":
            return "max_failures"

        if test_failed == 0:
            return "passed"
        if iteration >= MAX_REVIEW_ITERATIONS + MAX_TEST_FIX_ITERATIONS:
            return "max_failures"
        return "failed"

    # --- Node Wrapper ---

    async def _run_node(
        self,
        node_name: str,
        state: ALMState,
        agent_cls_path: str,
        method: str = "run",
    ) -> dict[str, Any]:
        """Wrapper that handles checkpointing, timeout, memory, and A2A persistence."""
        self._report_progress(state, node_name, "running", f"Starting {node_name}...")
        start = time.monotonic()

        # Save checkpoint before execution
        await self._checkpoint.save(state.get("run_id", ""), node_name, dict(state))

        # Import and instantiate agent
        import importlib

        module_path, class_name = agent_cls_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        agent_cls = getattr(module, class_name)
        agent = agent_cls(self.scm, self.state_manager, self.config)

        # Inject memory context
        await self._get_memory_context(agent.name, state)

        # Execute with timeout
        try:
            coro = agent.run(state) if method == "run" else getattr(agent, method)(state)

            result = await with_timeout(coro, NODE_TIMEOUT, f"agent:{node_name}")
        except TimeoutError:
            logger.error("Agent %s timed out after %ds", node_name, NODE_TIMEOUT)
            self._report_progress(state, node_name, "failed", f"Timed out after {NODE_TIMEOUT}s")
            result = {"error": f"Agent {node_name} timed out after {NODE_TIMEOUT}s"}
        except Exception as e:
            logger.exception("Agent %s failed: %s", node_name, e)
            self._report_progress(state, node_name, "failed", str(e)[:200])
            # Learn from the failure
            await self._record_failure(agent.name, state, str(e))
            raise

        elapsed = time.monotonic() - start

        # Persist A2A messages to Redis
        a2a_messages = result.get("a2a_messages", state.get("a2a_messages", []))
        if a2a_messages:
            await self._persist_a2a(state.get("run_id", ""), a2a_messages)

        # Record timing
        result["agent_timings"] = {
            **state.get("agent_timings", {}),
            node_name: elapsed,
        }

        self._report_progress(state, node_name, "completed", f"Done in {elapsed:.1f}s")

        # Learn from success
        await self._record_success(agent.name, state, node_name, elapsed)

        return result

    # --- Supervisor & Orchestrator Nodes ---

    @traceable_if_enabled(name="alm.supervisor", run_type="chain")
    async def _node_supervisor(self, state: ALMState) -> dict[str, Any]:
        """Supervisor plans the architecture and creates the delegation plan."""
        result = await self._run_node(
            "supervisor",
            state,
            "devai.agents.supervisor.SupervisorAgent",
        )
        result["stage"] = "plan_approved"
        return result

    @traceable_if_enabled(name="alm.orchestrator_pre", run_type="chain")
    async def _node_orchestrator_pre(self, state: ALMState) -> dict[str, Any]:
        """Orchestrator validates the plan and prepares for execution."""
        result = await self._run_node(
            "orchestrator_pre",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_review", run_type="chain")
    async def _node_orchestrator_review(self, state: ALMState) -> dict[str, Any]:
        """Orchestrator evaluates review results and decides next step."""
        result = await self._run_node(
            "orchestrator_review",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_security", run_type="chain")
    async def _node_orchestrator_security(self, state: ALMState) -> dict[str, Any]:
        """Orchestrator evaluates security scan results and decides next step."""
        result = await self._run_node(
            "orchestrator_security",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_tests", run_type="chain")
    async def _node_orchestrator_tests(self, state: ALMState) -> dict[str, Any]:
        """Orchestrator evaluates test results and decides next step."""
        result = await self._run_node(
            "orchestrator_tests",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_post", run_type="chain")
    async def _node_orchestrator_post(self, state: ALMState) -> dict[str, Any]:
        """Orchestrator produces the final pipeline status report."""
        result = await self._run_node(
            "orchestrator_post",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        result["stage"] = state.get("stage", "done")
        return result

    # --- Specialist Agent Nodes ---

    @traceable_if_enabled(name="alm.ingest_documents", run_type="chain")
    async def _node_ingest_documents(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "ingest_documents",
            state,
            "devai.agents.document_analyzer.DocumentAnalyzerAgent",
        )
        result["stage"] = "requirements_analyzed"
        return result

    @traceable_if_enabled(name="alm.detect_tech_stack", run_type="chain")
    async def _node_detect_tech_stack(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "detect_tech_stack",
            state,
            "devai.agents.tech_detector.TechDetectorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.analyze_requirements", run_type="chain")
    async def _node_analyze_requirements(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "analyze_requirements",
            state,
            "devai.agents.requirements_analyst.RequirementsAnalystAgent",
        )
        result["stage"] = "requirements_analyzed"
        return result

    @traceable_if_enabled(name="alm.create_epic", run_type="chain")
    async def _node_create_epic(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "create_epic",
            state,
            "devai.agents.product_director.ProductDirectorAgent",
            method="run_epic",
        )
        result["stage"] = "epic_created"
        return result

    @traceable_if_enabled(name="alm.create_stories", run_type="chain")
    async def _node_create_stories(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "create_stories",
            state,
            "devai.agents.product_director.ProductDirectorAgent",
            method="run_stories",
        )
        result["stage"] = "stories_created"
        return result

    @traceable_if_enabled(name="alm.create_plan", run_type="chain")
    async def _node_create_plan(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "create_plan",
            state,
            "devai.agents.engineering_manager.EngineeringManagerAgent",
        )
        result["stage"] = "plan_created"
        return result

    @traceable_if_enabled(name="alm.implement_code", run_type="chain")
    async def _node_implement_code(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "implement_code",
            state,
            "devai.agents.senior_developer.SeniorDeveloperAgent",
        )
        result["stage"] = "code_implemented"
        return result

    @traceable_if_enabled(name="alm.db_engineering", run_type="chain")
    async def _node_db_engineering(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "db_engineering",
            state,
            "devai.agents.db_engineer.DBEngineerAgent",
        )
        return result

    @traceable_if_enabled(name="alm.review_code", run_type="chain")
    async def _node_review_code(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "review_code",
            state,
            "devai.agents.staff_reviewer.StaffReviewerAgent",
        )
        result["stage"] = "code_reviewed"
        return result

    @traceable_if_enabled(name="alm.security_scan", run_type="chain")
    async def _node_security_scan(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "security_scan",
            state,
            "devai.agents.security_expert.SecurityExpertAgent",
        )
        return result

    @traceable_if_enabled(name="alm.monitor_build", run_type="chain")
    async def _node_monitor_build(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "monitor_build",
            state,
            "devai.agents.ci_monitor.CIMonitorAgent",
        )
        result["stage"] = "build_monitoring"
        return result

    @traceable_if_enabled(name="alm.run_tests", run_type="chain")
    async def _node_run_tests(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "run_tests",
            state,
            "devai.agents.qa_tester.QATesterAgent",
        )
        result["stage"] = "tests_complete"
        return result

    @traceable_if_enabled(name="alm.provision_infra", run_type="chain")
    async def _node_provision_infra(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "provision_infra",
            state,
            "devai.agents.infra_provisioner.InfraProvisionerAgent",
        )
        return result

    @traceable_if_enabled(name="alm.deploy_release", run_type="chain")
    async def _node_deploy_release(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "deploy_release",
            state,
            "devai.agents.release_manager.ReleaseManagerAgent",
        )
        result["stage"] = "deployed"
        return result

    # --- Memory Integration ---

    async def _get_memory_context(self, agent_name: str, state: ALMState) -> str:
        """Load relevant memories for the agent."""
        try:
            from devai.services.memory import AgentMemory

            memory = AgentMemory(self.state_manager.redis)
            return await memory.build_context(
                agent=agent_name,
                repo=state.get("repo_full_name", ""),
                query=state.get("requirements", "")[:200],
                limit=5,
            )
        except Exception:
            return ""

    async def _record_success(
        self,
        agent_name: str,
        state: ALMState,
        node_name: str,
        elapsed: float,
    ) -> None:
        """Record successful execution in memory."""
        try:
            from devai.services.memory import AgentMemory

            memory = AgentMemory(self.state_manager.redis)
            await memory.remember(
                agent=agent_name,
                content=f"Successfully executed {node_name} in {elapsed:.1f}s for {state.get('repo_full_name', '')}",
                memory_type="episodic",
                repo=state.get("repo_full_name", "global"),
                tags=["success", node_name],
                metadata={"run_id": state.get("run_id", ""), "elapsed": elapsed},
            )
        except Exception:
            pass

    async def _record_failure(self, agent_name: str, state: ALMState, error: str) -> None:
        """Record failure in memory for future learning."""
        try:
            from devai.services.memory import AgentMemory

            memory = AgentMemory(self.state_manager.redis)
            await memory.remember(
                agent=agent_name,
                content=f"Failed on {state.get('repo_full_name', '')}: {error[:200]}",
                memory_type="episodic",
                repo=state.get("repo_full_name", "global"),
                tags=["failure", agent_name],
                metadata={"run_id": state.get("run_id", ""), "error": error[:500]},
            )
        except Exception:
            pass

    # --- A2A Persistence ---

    async def _persist_a2a(self, run_id: str, messages: list) -> None:
        """Persist A2A messages to Redis."""
        try:
            pipe = self.state_manager.redis.pipeline()
            for msg in messages:
                pipe.rpush(f"devai:run:{run_id}:a2a_messages", json.dumps(msg, default=str))
            pipe.expire(f"devai:run:{run_id}:a2a_messages", 86400 * 30)
            await pipe.execute()
        except Exception as e:
            logger.warning("Failed to persist A2A messages: %s", e)

    # --- Progress Reporting ---

    def _report_progress(self, state: ALMState, step: str, status: str, detail: str) -> None:
        callback = state.get("on_progress")
        if callback and callable(callback):
            with contextlib.suppress(Exception):
                callback(step, status, detail)
        logger.info("Pipeline [%s] %s: %s — %s", state.get("run_id", "?"), step, status, detail)
        # Persist event to Redis for dashboard visibility
        run_id = state.get("run_id", "")
        if run_id:
            import asyncio

            asyncio.ensure_future(self._persist_event(run_id, step, status, detail))

    async def _persist_event(self, run_id: str, step: str, status: str, detail: str) -> None:
        """Persist a pipeline event to Redis for real-time dashboard display."""
        try:
            event = json.dumps(
                {
                    "step": step,
                    "status": status,
                    "detail": detail,
                    "timestamp": time.time(),
                },
                default=str,
            )
            pipe = self.state_manager.redis.pipeline()
            pipe.rpush(f"devai:run:{run_id}:events", event)
            pipe.expire(f"devai:run:{run_id}:events", 86400 * 7)
            # Keep last 200 events
            pipe.ltrim(f"devai:run:{run_id}:events", -200, -1)
            await pipe.execute()
        except Exception:
            pass  # Best-effort event logging

    # --- Public API ---

    @traceable_if_enabled(name="alm_pipeline.run", run_type="chain")
    async def run(
        self,
        repo_full_name: str,
        requirements: str,
        trigger_type: str = "cli",
        trigger_ref: str = "cli",
        on_progress: Any = None,
        resume_from: str | None = None,
    ) -> ALMState:
        """Run the full ALM pipeline.

        Args:
            repo_full_name: GitHub repo (org/repo).
            requirements: Raw requirements text, document paths, or URLs.
            trigger_type: How the pipeline was triggered.
            trigger_ref: Reference to the trigger source.
            on_progress: Optional callback(step, status, detail).
            resume_from: Optional run_id to resume from last checkpoint.
        """
        from ulid import ULID

        # Resume from checkpoint if requested
        if resume_from:
            checkpoint_state = await self._checkpoint.load(resume_from)
            if checkpoint_state:
                logger.info("Resuming pipeline from checkpoint: %s", resume_from)
                checkpoint_state["on_progress"] = on_progress
                final_state = await self._graph.ainvoke(checkpoint_state)
                return final_state

        initial_state: ALMState = {
            "run_id": str(ULID()),
            "repo_full_name": repo_full_name,
            "trigger_type": trigger_type,
            "trigger_ref": trigger_ref,
            "requirements": requirements,
            "stage": "triggered",
            "a2a_messages": [],
            "review_iteration": 0,
            "review_feedback": [],
            "agent_timings": {},
            "error": None,
            "on_progress": on_progress,
        }

        # Load governance (CLAUDE.md)
        governance = await self.state_manager.redis.get(f"devai:governance:{repo_full_name}:claude_md")
        if governance:
            initial_state["governance"] = governance

        # Persist the initial run
        from devai.models import PipelineContext, TriggerType

        ctx = PipelineContext(
            run_id=initial_state["run_id"],
            repo_full_name=repo_full_name,
            trigger_type=TriggerType(trigger_type),
            trigger_ref=trigger_ref,
            requirements=requirements,
        )
        await self.state_manager.create_run(ctx)

        logger.info(
            "ALM pipeline started: run_id=%s repo=%s",
            initial_state["run_id"],
            repo_full_name,
        )

        try:
            final_state = await self._graph.ainvoke(initial_state)

            final_stage = final_state.get("stage", "done")
            await self.state_manager.update_run_stage(initial_state["run_id"], final_stage)

            # Clean up checkpoints on success
            await self._checkpoint.cleanup(initial_state["run_id"])

            logger.info(
                "ALM pipeline completed: run_id=%s stage=%s timings=%s",
                initial_state["run_id"],
                final_stage,
                final_state.get("agent_timings", {}),
            )

            return final_state

        except Exception:
            logger.exception("ALM pipeline failed: run_id=%s", initial_state["run_id"])
            await self.state_manager.update_run_stage(initial_state["run_id"], "failed")
            raise
