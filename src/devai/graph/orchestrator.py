"""LangGraph ALM Pipeline Orchestrator — Collaborative Parallel Model.

Full Application Lifecycle Management pipeline with story-level parallelism:

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
    create_plan ─────── (EM creates per-story technical plans)
      |
    ┌─ story_loop_start ── (picks next unprocessed story, loads context)
    │    |
    │  implement_story ── (Sr Dev creates feature branch, implements story)
    │    |
    │  db_engineering_story
    │    |
    │  review_story ────→ orchestrator_review → changes → implement_story
    │    | (approved)
    │  security_scan_story → orchestrator_security → block → implement_story
    │    | (pass)
    │  test_story ──────→ orchestrator_tests → failed → implement_story
    │    | (passed)
    │  story_complete ──→ (marks story approved, saves to story_branches)
    │    |
    └── story_loop_check → more stories? → story_loop_start
          |
          all done
          |
    merge_stories ────── (Release Manager merges all story PRs to main)
      |
    monitor_build
      |
    provision_infra
      |
    deploy_release
      |
    orchestrator_post ── (final status report)
      |
    END

Features:
- Each story gets its own feature branch and PR
- Story-level quality gates (review, security, tests)
- Agents collaborate via A2A throughout the pipeline
- State checkpointing at each stage boundary
- Agent memory injection for cross-run learning
- Per-node timeout protection (15 min)
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, StateGraph

from devai.graph.state import ALMState, StoryBranchDict
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
    """LangGraph-based ALM pipeline orchestrator with collaborative story-level execution."""

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
        """Build the ALM StateGraph with story-level parallel execution."""
        graph = StateGraph(ALMState)

        # --- Coordination layer ---
        graph.add_node("supervisor", self._node_supervisor)
        graph.add_node("orchestrator_pre", self._node_orchestrator_pre)
        graph.add_node("orchestrator_review", self._node_orchestrator_review)
        graph.add_node("orchestrator_security", self._node_orchestrator_security)
        graph.add_node("orchestrator_tests", self._node_orchestrator_tests)
        graph.add_node("orchestrator_post", self._node_orchestrator_post)

        # --- Planning phase (linear) ---
        graph.add_node("ingest_documents", self._node_ingest_documents)
        graph.add_node("detect_tech_stack", self._node_detect_tech_stack)
        graph.add_node("analyze_requirements", self._node_analyze_requirements)
        graph.add_node("create_epic", self._node_create_epic)
        graph.add_node("create_stories", self._node_create_stories)
        graph.add_node("create_plan", self._node_create_plan)

        # --- Story loop nodes ---
        graph.add_node("story_loop_start", self._node_story_loop_start)
        graph.add_node("implement_story", self._node_implement_story)
        graph.add_node("db_engineering_story", self._node_db_engineering_story)
        graph.add_node("review_story", self._node_review_story)
        graph.add_node("security_scan_story", self._node_security_scan_story)
        graph.add_node("test_story", self._node_test_story)
        graph.add_node("story_complete", self._node_story_complete)

        # --- Deployment phase ---
        graph.add_node("merge_stories", self._node_merge_stories)
        graph.add_node("monitor_build", self._node_monitor_build)
        graph.add_node("provision_infra", self._node_provision_infra)
        graph.add_node("deploy_release", self._node_deploy_release)

        # --- Define edges ---

        # Entry: Supervisor plans first
        graph.set_entry_point("supervisor")

        # Planning phase (linear)
        graph.add_edge("supervisor", "orchestrator_pre")
        graph.add_edge("orchestrator_pre", "ingest_documents")
        graph.add_edge("ingest_documents", "detect_tech_stack")
        graph.add_edge("detect_tech_stack", "analyze_requirements")
        graph.add_edge("analyze_requirements", "create_epic")
        graph.add_edge("create_epic", "create_stories")
        graph.add_edge("create_stories", "create_plan")

        # After planning → start story loop
        graph.add_edge("create_plan", "story_loop_start")

        # Story implementation cycle
        graph.add_edge("story_loop_start", "implement_story")
        graph.add_edge("implement_story", "db_engineering_story")
        graph.add_edge("db_engineering_story", "review_story")

        # Review gate: orchestrator decides
        graph.add_edge("review_story", "orchestrator_review")
        graph.add_conditional_edges(
            "orchestrator_review",
            self._route_after_review,
            {
                "approved": "security_scan_story",
                "changes_requested": "implement_story",
                "max_iterations": "story_complete",
            },
        )

        # Security gate: orchestrator decides
        graph.add_edge("security_scan_story", "orchestrator_security")
        graph.add_conditional_edges(
            "orchestrator_security",
            self._route_after_security,
            {
                "pass": "test_story",
                "pass_with_warnings": "test_story",
                "block": "implement_story",
                "max_blocks": "story_complete",
            },
        )

        # Test gate: orchestrator decides
        graph.add_edge("test_story", "orchestrator_tests")
        graph.add_conditional_edges(
            "orchestrator_tests",
            self._route_after_tests,
            {
                "passed": "story_complete",
                "failed": "implement_story",
                "max_failures": "story_complete",
            },
        )

        # After story complete: check if more stories remain
        graph.add_conditional_edges(
            "story_complete",
            self._route_story_loop,
            {
                "next_story": "story_loop_start",
                "all_done": "merge_stories",
            },
        )

        # Deployment phase (after all stories done)
        graph.add_edge("merge_stories", "monitor_build")
        graph.add_edge("monitor_build", "provision_infra")
        graph.add_edge("provision_infra", "deploy_release")
        graph.add_edge("deploy_release", "orchestrator_post")
        graph.add_edge("orchestrator_post", END)

        return graph.compile()

    # ------------------------------------------------------------------
    # Routing Functions
    # ------------------------------------------------------------------

    def _route_after_review(self, state: ALMState) -> str:
        """Route after code review for the active story."""
        decision = state.get("review_decision", "changes_requested")
        iteration = state.get("review_iteration", 0)

        routing = state.get("orchestrator_routing", {})
        if routing.get("decision") == "escalate":
            logger.warning("Orchestrator escalated at review stage")
            return "max_iterations"

        if decision == "approved":
            return "approved"
        if iteration >= MAX_REVIEW_ITERATIONS:
            logger.warning("Max review iterations (%d) reached for story", MAX_REVIEW_ITERATIONS)
            return "max_iterations"
        return "changes_requested"

    def _route_after_security(self, state: ALMState) -> str:
        """Route after security scan for the active story."""
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
        """Route after test execution for the active story."""
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

    def _route_story_loop(self, state: ALMState) -> str:
        """Check if more stories need processing, or all are done."""
        story_branches = state.get("story_branches", [])
        for sb in story_branches:
            if sb.get("status") == "pending":
                return "next_story"
        return "all_done"

    # ------------------------------------------------------------------
    # Story Loop Management Nodes
    # ------------------------------------------------------------------

    @traceable_if_enabled(name="alm.story_loop_start", run_type="chain")
    async def _node_story_loop_start(self, state: ALMState) -> dict[str, Any]:
        """Pick the next unprocessed story and load its context into active state.

        Also checks for injected requirements from the dashboard chat —
        if found, appends them to the requirements so agents see them.
        """
        # Check for injected requirements from dashboard/chat
        run_id = state.get("run_id", "")
        injected_reqs = await self._check_injections(run_id)
        extra_state: dict[str, Any] = {}
        if injected_reqs:
            current_reqs = state.get("requirements", "")
            injection_text = "\n\n## Additional Requirements (from user)\n" + "\n".join(
                f"- {inj}" for inj in injected_reqs
            )
            extra_state["requirements"] = current_reqs + injection_text
            self._report_progress(
                state,
                "story_loop_start",
                "info",
                f"Picked up {len(injected_reqs)} injected requirement(s) from user",
            )

        story_branches = list(state.get("story_branches", []))
        stories = state.get("stories", [])
        story_plans = state.get("story_plans", [])

        # Find the next pending story
        next_index = None
        for sb in story_branches:
            if sb.get("status") == "pending":
                next_index = sb["story_index"]
                break

        if next_index is None:
            # All stories done — shouldn't reach here, but handle gracefully
            return {"stage": "all_stories_complete"}

        # Mark story as implementing
        story_branches[next_index] = {**story_branches[next_index], "status": "implementing"}

        story = stories[next_index] if next_index < len(stories) else {}
        story_plan = story_plans[next_index] if next_index < len(story_plans) else ""

        story_num = story.get("number", "?")
        story_title = story.get("title", "Unknown")

        self._report_progress(
            state,
            "story_loop_start",
            "running",
            f"Starting story {next_index + 1}/{len(story_branches)}: #{story_num} — {story_title}",
        )

        # Load story context into active flat fields
        # Check if story was previously attempted (has branch/PR from prior iteration)
        sb = story_branches[next_index]

        result = {
            "active_story_index": next_index,
            "story_branches": story_branches,
            # Reset active context for this story
            "branch_name": sb.get("branch_name"),
            "pr_number": sb.get("pr_number"),
            "review_decision": "",
            "review_summary": "",
            "review_comments": [],
            "review_feedback": sb.get("review_feedback", []),
            "review_iteration": sb.get("review_iteration", 0),
            "security_decision": "",
            "security_summary": "",
            "security_findings": [],
            "security_issues": [],
            "performance_issues": [],
            "test_failed": 0,
            "test_summary": "",
            "test_failures": [],
            "implementation_summary": "",
            "db_decision": "",
            "db_summary": "",
            # Set story-specific plan as the technical plan for agents to read
            "technical_plan": story_plan,
            "stage": f"story_{next_index + 1}_implementing",
            **extra_state,
        }
        return result

    @traceable_if_enabled(name="alm.story_complete", run_type="chain")
    async def _node_story_complete(self, state: ALMState) -> dict[str, Any]:
        """Save current story results back to story_branches and mark done."""
        idx = state.get("active_story_index", 0)
        story_branches = list(state.get("story_branches", []))

        if idx >= len(story_branches):
            return {}

        # Determine final status for this story
        review_decision = state.get("review_decision", "")
        security_decision = state.get("security_decision", "pass")
        test_failed = state.get("test_failed", 0)

        if review_decision == "approved" and security_decision in ("pass", "pass_with_warnings") and test_failed == 0:
            final_status = "approved"
        else:
            final_status = "failed"

        # Save all results back to this story's branch record
        story_branches[idx] = {
            **story_branches[idx],
            "status": final_status,
            "branch_name": state.get("branch_name"),
            "pr_number": state.get("pr_number"),
            "review_decision": review_decision,
            "review_iteration": state.get("review_iteration", 0),
            "review_feedback": state.get("review_feedback", []),
            "security_decision": security_decision,
            "test_failed": test_failed,
            "test_summary": state.get("test_summary", ""),
            "implementation_summary": state.get("implementation_summary", ""),
        }

        story = state.get("stories", [])[idx] if idx < len(state.get("stories", [])) else {}
        story_num = story.get("number", "?")

        self._report_progress(
            state,
            "story_complete",
            "completed",
            f"Story #{story_num} completed with status: {final_status}",
        )

        return {
            "story_branches": story_branches,
            "stage": f"story_{idx + 1}_{final_status}",
        }

    # ------------------------------------------------------------------
    # Node Wrapper
    # ------------------------------------------------------------------

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
        await self.state_manager.set_agent_status(state.get("run_id", ""), agent.name, "running")

        # Inject memory context
        memory_context = await self._get_memory_context(agent.name, state)
        execution_state = dict(state)
        if memory_context:
            execution_state["memory_context"] = memory_context

        # Execute with timeout
        try:
            coro = agent.run(execution_state) if method == "run" else getattr(agent, method)(execution_state)

            result = await with_timeout(coro, NODE_TIMEOUT, f"agent:{node_name}")
        except TimeoutError:
            logger.error("Agent %s timed out after %ds", node_name, NODE_TIMEOUT)
            self._report_progress(state, node_name, "failed", f"Timed out after {NODE_TIMEOUT}s")
            await self.state_manager.set_agent_status(
                state.get("run_id", ""),
                agent.name,
                "failed",
                f"Timed out after {NODE_TIMEOUT}s",
            )
            result = {"error": f"Agent {node_name} timed out after {NODE_TIMEOUT}s"}
        except Exception as e:
            logger.exception("Agent %s failed: %s", node_name, e)
            self._report_progress(state, node_name, "failed", str(e)[:200])
            await self.state_manager.set_agent_status(state.get("run_id", ""), agent.name, "failed", str(e)[:500])
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

        if result.get("error"):
            await self.state_manager.set_agent_status(
                state.get("run_id", ""),
                agent.name,
                "failed",
                str(result["error"])[:500],
            )
            self._report_progress(state, node_name, "failed", str(result["error"])[:200])
            await self._record_failure(agent.name, state, str(result["error"]))
        else:
            await self.state_manager.set_agent_status(state.get("run_id", ""), agent.name, "completed")
            self._report_progress(state, node_name, "completed", f"Done in {elapsed:.1f}s")
            # Learn from success
            await self._record_success(agent.name, state, node_name, elapsed)

        return result

    # ------------------------------------------------------------------
    # Supervisor & Orchestrator Nodes
    # ------------------------------------------------------------------

    @traceable_if_enabled(name="alm.supervisor", run_type="chain")
    async def _node_supervisor(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "supervisor",
            state,
            "devai.agents.supervisor.SupervisorAgent",
        )
        result["stage"] = "plan_approved"
        return result

    @traceable_if_enabled(name="alm.orchestrator_pre", run_type="chain")
    async def _node_orchestrator_pre(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "orchestrator_pre",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_review", run_type="chain")
    async def _node_orchestrator_review(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "orchestrator_review",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_security", run_type="chain")
    async def _node_orchestrator_security(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "orchestrator_security",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_tests", run_type="chain")
    async def _node_orchestrator_tests(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "orchestrator_tests",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        return result

    @traceable_if_enabled(name="alm.orchestrator_post", run_type="chain")
    async def _node_orchestrator_post(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "orchestrator_post",
            state,
            "devai.agents.orchestrator.OrchestratorAgent",
        )
        result["stage"] = state.get("stage", "done")
        return result

    # ------------------------------------------------------------------
    # Planning Phase Nodes (linear — same as before)
    # ------------------------------------------------------------------

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

        # Initialize story_branches from newly created stories
        stories = result.get("stories", state.get("stories", []))
        story_branches: list[StoryBranchDict] = []
        for i, story in enumerate(stories):
            story_branches.append(
                StoryBranchDict(
                    story_index=i,
                    story_number=story.get("number", 0),
                    story_title=story.get("title", ""),
                    branch_name=None,
                    pr_number=None,
                    status="pending",
                    review_decision="",
                    review_iteration=0,
                    review_feedback=[],
                    security_decision="",
                    test_failed=0,
                    test_summary="",
                    implementation_summary="",
                    story_plan="",
                )
            )
        result["story_branches"] = story_branches
        result["active_story_index"] = 0

        return result

    @traceable_if_enabled(name="alm.create_plan", run_type="chain")
    async def _node_create_plan(self, state: ALMState) -> dict[str, Any]:
        """EM creates per-story technical plans."""
        result = await self._run_node(
            "create_plan",
            state,
            "devai.agents.engineering_manager.EngineeringManagerAgent",
        )
        result["stage"] = "plan_created"

        # Store per-story plans in story_branches
        story_plans = result.get("story_plans", [])
        if story_plans:
            story_branches = list(state.get("story_branches", []))
            for i, plan in enumerate(story_plans):
                if i < len(story_branches):
                    story_branches[i] = {**story_branches[i], "story_plan": plan}
            result["story_branches"] = story_branches

        return result

    # ------------------------------------------------------------------
    # Story-Level Execution Nodes
    # ------------------------------------------------------------------

    @traceable_if_enabled(name="alm.implement_story", run_type="chain")
    async def _node_implement_story(self, state: ALMState) -> dict[str, Any]:
        """Implement the active story on its own feature branch."""
        result = await self._run_node(
            "implement_story",
            state,
            "devai.agents.senior_developer.SeniorDeveloperAgent",
        )
        result["stage"] = "code_implemented"
        return result

    @traceable_if_enabled(name="alm.db_engineering_story", run_type="chain")
    async def _node_db_engineering_story(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "db_engineering_story",
            state,
            "devai.agents.db_engineer.DBEngineerAgent",
        )
        return result

    @traceable_if_enabled(name="alm.review_story", run_type="chain")
    async def _node_review_story(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "review_story",
            state,
            "devai.agents.staff_reviewer.StaffReviewerAgent",
        )
        result["stage"] = "code_reviewed"
        return result

    @traceable_if_enabled(name="alm.security_scan_story", run_type="chain")
    async def _node_security_scan_story(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "security_scan_story",
            state,
            "devai.agents.security_expert.SecurityExpertAgent",
        )
        return result

    @traceable_if_enabled(name="alm.test_story", run_type="chain")
    async def _node_test_story(self, state: ALMState) -> dict[str, Any]:
        result = await self._run_node(
            "test_story",
            state,
            "devai.agents.qa_tester.QATesterAgent",
        )
        result["stage"] = "tests_complete"
        return result

    # ------------------------------------------------------------------
    # Deployment Phase Nodes
    # ------------------------------------------------------------------

    @traceable_if_enabled(name="alm.merge_stories", run_type="chain")
    async def _node_merge_stories(self, state: ALMState) -> dict[str, Any]:
        """Release Manager merges all approved story PRs to main."""
        result = await self._run_node(
            "merge_stories",
            state,
            "devai.agents.release_manager.ReleaseManagerAgent",
            method="run_merge_stories",
        )
        result["stage"] = "stories_merged"
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

    # ------------------------------------------------------------------
    # Injection Polling (human-in-the-loop requirements)
    # ------------------------------------------------------------------

    async def _check_injections(self, run_id: str) -> list[str]:
        """Check Redis for requirements injected by the user via dashboard chat.

        Reads and clears the injection queue so each injection is processed once.
        """
        if not run_id:
            return []
        try:
            key = f"devai:run:{run_id}:injections"
            raw_items = await self.state_manager.redis.lrange(key, 0, -1)
            if not raw_items:
                return []

            # Clear the queue
            await self.state_manager.redis.delete(key)

            messages = []
            for raw in raw_items:
                try:
                    data = json.loads(raw)
                    messages.append(data.get("message", ""))
                except (json.JSONDecodeError, KeyError):
                    pass

            if messages:
                logger.info("Found %d injected requirement(s) for run %s", len(messages), run_id)
            return messages
        except Exception as e:
            logger.warning("Failed to check injections for run %s: %s", run_id, e)
            return []

    # ------------------------------------------------------------------
    # Memory Integration
    # ------------------------------------------------------------------

    async def _get_memory_context(self, agent_name: str, state: ALMState) -> str:
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

    # ------------------------------------------------------------------
    # A2A Persistence
    # ------------------------------------------------------------------

    async def _persist_a2a(self, run_id: str, messages: list) -> None:
        try:
            pipe = self.state_manager.redis.pipeline()
            for msg in messages:
                pipe.rpush(f"devai:run:{run_id}:a2a_messages", json.dumps(msg, default=str))
            pipe.expire(f"devai:run:{run_id}:a2a_messages", 86400 * 30)
            await pipe.execute()
        except Exception as e:
            logger.warning("Failed to persist A2A messages: %s", e)

    # ------------------------------------------------------------------
    # Progress Reporting
    # ------------------------------------------------------------------

    def _report_progress(self, state: ALMState, step: str, status: str, detail: str) -> None:
        # Include story context in progress reports
        idx = state.get("active_story_index")
        story_branches = state.get("story_branches", [])
        if idx is not None and idx < len(story_branches):
            sb = story_branches[idx]
            detail = f"[Story #{sb.get('story_number', '?')}] {detail}"

        callback = state.get("on_progress")
        if callback and callable(callback):
            with contextlib.suppress(Exception):
                callback(step, status, detail)
        logger.info("Pipeline [%s] %s: %s — %s", state.get("run_id", "?"), step, status, detail)
        run_id = state.get("run_id", "")
        if run_id:
            import asyncio

            asyncio.ensure_future(self._persist_event(run_id, step, status, detail))

    async def _persist_event(self, run_id: str, step: str, status: str, detail: str) -> None:
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
            pipe.ltrim(f"devai:run:{run_id}:events", -200, -1)
            await pipe.execute()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """Run the full ALM pipeline with story-level parallel execution.

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
            "story_branches": [],
            "active_story_index": 0,
            "story_plans": [],
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
