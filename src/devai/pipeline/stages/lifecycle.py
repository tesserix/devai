"""Deterministic lifecycle stages — no agent calls.

These are the Fiber `lifecycle` family: create_issue, detect_pr, cleanup,
post_report, plus a couple of context-shaping stages (context_hydration,
memory_injection) and a noop for blueprint testing.

Each function in this module is a stage factory. The function returns a
PipelineStage instance configured with the StageDeps + per-stage config.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.stages._base import run_correlation_label
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────────────


class _NoopStage(PipelineStage):
    """Returns an empty StageResult. Useful for blueprint scaffolding."""

    def __init__(self, key: str, message: str = "") -> None:
        self._key = key
        self._message = message or f"{key} (noop)"

    def name(self) -> str:
        return self._key

    async def execute(self, task: DevAITask) -> StageResult:
        return StageResult(message=self._message)


def noop_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _NoopStage(config.get("key", "noop"), config.get("message", ""))


# ──────────────────────────────────────────────────────────────────────
# create_issue
# ──────────────────────────────────────────────────────────────────────


class _CreateIssueStage(PipelineStage):
    """Open a GitHub issue for this task.

    Uses the SCM client when available, otherwise records a stub issue
    so downstream stages can pretend an issue exists. The intent text
    becomes the issue body.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return "create_issue"

    async def execute(self, task: DevAITask) -> StageResult:
        if self.deps.scm is None:
            logger.info("create_issue: no SCM client — recording stub issue")
            task.issue_number = -1  # sentinel for "stub"
            return StageResult(
                message="stub issue (no SCM client)",
                data={"issue_url": "", "issue_number": -1, "create_issue_stub": True},
            )

        title = self.config.get("title_template", "[devai] {intent}").format(intent=task.intent[:80])
        body = f"Triggered by DevAI pipeline run `{task.id}`.\n\n{task.intent}"

        try:
            # SCM clients expose create_issue_idempotent(repo, title, body, labels=...)
            issue = await self.deps.scm.create_issue_idempotent(  # type: ignore[union-attr]
                task.repo,
                title=title,
                body=body,
                labels=(
                    [self.deps.config.pipeline_label] if hasattr(self.deps.config, "pipeline_label") else []
                )
                + [run_correlation_label(task.id)],
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("create_issue: SCM call failed")
            return StageResult(
                message=f"create_issue failed: {e}",
                data={"create_issue_error": str(e)},
            )

        number = getattr(issue, "number", None) or (issue.get("number") if isinstance(issue, dict) else None)
        url = getattr(issue, "url", "") or (issue.get("url", "") if isinstance(issue, dict) else "")
        if isinstance(number, int):
            task.issue_number = number

        return StageResult(
            message=f"created issue #{number}",
            data={"issue_number": number, "issue_url": url},
        )


def create_issue_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreateIssueStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# detect_pr — poll for the PR a coding stage pushed
# ──────────────────────────────────────────────────────────────────────


class _DetectPRStage(PipelineStage):
    """Poll the SCM until a PR exists on the task's branch.

    The current ALM pipeline writes the PR number directly from the
    SeniorDeveloper agent. This stage is for blueprints where the agent
    pushes via gh CLI inside a sandbox and the orchestrator has to find
    it after the fact.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.timeout_seconds = float(config.get("timeout_seconds", "300"))
        self.poll_interval = float(config.get("poll_interval", "5"))

    def name(self) -> str:
        return "detect_pr"

    async def execute(self, task: DevAITask) -> StageResult:
        if task.pr_number is not None:
            return StageResult(message=f"PR #{task.pr_number} already detected", data={"pr_number": task.pr_number})
        if self.deps.scm is None or task.branch_name is None:
            return StageResult(message="no SCM client or branch — skipping detect_pr", data={"detect_pr_stub": True})

        import asyncio

        deadline = asyncio.get_event_loop().time() + self.timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            try:
                pr = await self.deps.scm.find_pr_by_branch(task.repo, task.branch_name)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pr = None
            if pr is not None:
                num = getattr(pr, "number", None) or (pr.get("number") if isinstance(pr, dict) else None)
                if isinstance(num, int):
                    task.pr_number = num
                    return StageResult(message=f"detected PR #{num}", data={"pr_number": num})
            await asyncio.sleep(self.poll_interval)

        return StageResult(message="timed out waiting for PR", data={"detect_pr_timeout": True})


def detect_pr_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DetectPRStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# await_merge
# ──────────────────────────────────────────────────────────────────────


class _AwaitMergeStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.timeout_seconds = float(config.get("timeout_seconds", "3600"))
        self.poll_interval = float(config.get("poll_interval", "10"))

    def name(self) -> str:
        return "await_merge"

    async def execute(self, task: DevAITask) -> StageResult:
        if task.pr_number is None or self.deps.scm is None:
            return StageResult(
                message="no PR or no SCM client — skipping await_merge",
                data={"await_merge_stub": True},
            )

        import asyncio

        deadline = asyncio.get_event_loop().time() + self.timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            try:
                merged = await self.deps.scm.is_pr_merged(task.repo, task.pr_number)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                merged = False
            if merged:
                return StageResult(
                    next_state=TaskState.COMPLETED,
                    message=f"PR #{task.pr_number} merged",
                    data={"pr_merged": True},
                )
            await asyncio.sleep(self.poll_interval)

        return StageResult(
            next_state=TaskState.AWAITING_APPROVAL,
            message="await_merge timed out",
            data={"await_merge_timeout": True},
        )


def await_merge_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _AwaitMergeStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# cleanup — release per-task resources (port-forwards, sandbox pods, …)
# ──────────────────────────────────────────────────────────────────────


class _CleanupStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps

    def name(self) -> str:
        return "cleanup"

    async def execute(self, task: DevAITask) -> StageResult:
        # Tear down the per-run preview pod (Deployment + Service +
        # VirtualService + PVC) if this run spun one up. Previously this was a
        # stub, so previews leaked until the namespace quota self-DoSed
        # (CODE-11). The deployment name is stamped on the task by the
        # spin_preview_pod stage (task.sandbox_pod / agent_context).
        cleaned_preview = await self._delete_preview(task)

        task.sandbox_pod = None
        task.dev_server_port = None
        return StageResult(
            message="cleanup complete",
            data={"cleanup_done": True, "preview_deleted": cleaned_preview},
        )

    async def _delete_preview(self, task: DevAITask) -> bool:
        """Best-effort delete of this run's preview. Returns True if attempted.

        The K8s runtime is shared on ``deps.extra["k8s_runtime"]`` (None when
        the runtime is disabled — local/in-process runs have no preview pod).
        Cleanup must never fail the run, so any error is logged and swallowed.
        """
        deployment = (
            task.sandbox_pod
            or task.agent_context.get("preview_deployment_name")
            or task.agent_context.get("preview_deployment")
        )
        if not deployment:
            return False

        runtime = (self.deps.extra or {}).get("k8s_runtime") if self.deps.extra else None
        if runtime is None:
            logger.debug("cleanup: preview %s present but no k8s runtime — skipping delete", deployment)
            return False

        try:
            await runtime.delete_preview(str(deployment))
            logger.info("cleanup: deleted preview %s for run %s", deployment, task.id)
            return True
        except Exception:  # noqa: BLE001 — cleanup must not fail the run
            logger.warning("cleanup: delete_preview(%s) failed", deployment, exc_info=True)
            return False


def cleanup_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CleanupStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# context_hydration / memory_injection
# ──────────────────────────────────────────────────────────────────────


class _ContextHydrationStage(PipelineStage):
    """Stub for Fiber's context_hydration — load repo profile, conventions,
    recent commits and put them on the task. Currently best-effort: if a
    StateManager-backed cache is wired up it'll use that, otherwise it's
    a no-op.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.max_bytes = int(config.get("max_bytes", "50000"))

    def name(self) -> str:
        return "context_hydration"

    async def execute(self, task: DevAITask) -> StageResult:
        if self.deps.scm is None:
            return StageResult(message="no SCM client — skipping hydration", data={"hydration_stub": True})

        try:
            profile = await self.deps.scm.get_repo_profile(task.repo)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            logger.debug("context_hydration: get_repo_profile failed", exc_info=True)
            profile = None

        if profile is None:
            return StageResult(message="no repo profile available", data={"hydration_stub": True})

        return StageResult(
            message=f"hydrated repo context ({len(str(profile))} bytes)",
            data={"repo_profile": profile, "hydrated_context": str(profile)[: self.max_bytes]},
        )


def context_hydration_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ContextHydrationStage(deps, config)


class _MemoryInjectionStage(PipelineStage):
    """Pull relevant prior-run memories onto the task before downstream
    stages run.

    Source of memories, in priority order:
      1. `deps.memory` — a `MemoryAdapter` selected via `DEVAI_MEMORY_PROVIDER`.
         This is the preferred path; provider can be mem0/zep/pgvector/redis.
      2. `deps.state_manager.memory` — legacy hook on older StateManager
         subclasses. Kept for backward compat with the LangGraph path.
      3. Nothing — degrade to an empty memory_context. Downstream stages
         work fine without prior context; they're just blinder.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.k = int(config.get("k", "5"))

    def name(self) -> str:
        return "memory_injection"

    async def execute(self, task: DevAITask) -> StageResult:
        # ── Preferred path: MemoryAdapter ───────────────────────────
        if self.deps.memory is not None:
            try:
                records = await self.deps.memory.semantic_search(
                    query=task.intent or task.label or task.repo,
                    k=self.k,
                    repo=task.repo or None,
                )
            except Exception:  # noqa: BLE001
                logger.exception("memory adapter semantic_search failed")
                records = []
            context = "\n".join(r.content for r in records)
            return StageResult(
                message=f"injected {len(records)} memories via {self.deps.memory.provider_name}",
                data={
                    "memory_context": context,
                    "memories": [r.to_dict() for r in records],
                    "memory_provider": self.deps.memory.provider_name,
                },
            )

        # ── Legacy path: state_manager.memory ───────────────────────
        if self.deps.state_manager is None:
            return StageResult(data={"memory_context": ""})

        memory_obj = getattr(self.deps.state_manager, "memory", None)
        if memory_obj is None or not hasattr(memory_obj, "semantic_search"):
            return StageResult(data={"memory_context": ""})

        try:
            memories = await memory_obj.semantic_search(task.intent, k=self.k)
        except Exception:  # noqa: BLE001
            memories = []

        context = "\n".join(getattr(m, "content", str(m)) for m in memories)
        return StageResult(
            message=f"injected {len(memories)} memories (legacy path)",
            data={"memory_context": context, "memories": [str(m) for m in memories]},
        )


def memory_injection_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _MemoryInjectionStage(deps, config)


class _PlanApprovalStage(PipelineStage):
    """Smart plan gate — pause for a human ONLY when the request needs it.

    Runs right after planning, before implementation. Behavior by the run's
    autonomy mode (``agent_context['autonomy']`` → DEVAI_PIPELINE_DEFAULT_AUTONOMY):

      full   — never pause; note + continue.
      gated  — always pause with the full plan context.
      auto   — (default) one cheap LLM call judges whether the intent is
               specific enough to implement without confirmation. CLEAR →
               continue (audited). AMBIGUOUS → pause with a proper approval
               request: the epic, the technical plan, the detected tech
               stack, and the LLM's open questions — everything a human
               needs to say "yes, proceed exactly like this".

    The pause uses the same decision key the dashboard's Approve/Reject
    endpoints write (``devai:pipeline:gate:{task}:{stage-name}``) and
    registers a DYNAMIC gate in ``agent_context['dynamic_gates']`` so the
    approvals API surfaces it with its context. This mechanism is generic:
    any stage that needs human input can register a dynamic gate the same
    way.
    """

    _POLL_SECONDS = 3.0

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return str(self.config.get("__stage_name") or "plan-approval")

    async def execute(self, task: DevAITask) -> StageResult:
        autonomy = str(
            task.agent_context.get("autonomy")
            or getattr(self.deps.config, "pipeline_default_autonomy", "auto")
            or "auto"
        ).lower()
        # `require_approval: true` in the blueprint stage config ALWAYS pauses
        # for the human, regardless of autonomy — boardroom decisions and
        # other deliberate sign-offs must never self-approve.
        force_gate = str(self.config.get("require_approval", "")).lower() in ("1", "true", "yes")

        if autonomy == "full" and not force_gate:
            return StageResult(message="plan approval skipped (autonomy=full)", data={"plan_approved": "auto"})

        questions: list[str] = []
        if autonomy != "gated" and not force_gate:
            verdict, questions = await self._assess_clarity(task)
            if verdict == "clear":
                return StageResult(
                    message="intent judged CLEAR — proceeding without approval",
                    data={"plan_approved": "auto-clear"},
                )

        # Pause: build the approval request a human can actually decide on.
        request = self._build_request(task, questions)
        gates = task.agent_context.setdefault("dynamic_gates", [])
        if isinstance(gates, list):
            gates.append(request)

        decision = await self._await_decision(task)
        if decision == "approved":
            await self._record_approval_on_epic(task, request)
            return StageResult(
                message="plan approved by human — proceeding",
                data={"plan_approved": "human", "plan_approval_request": request},
            )
        task.error = "plan rejected at approval" if decision == "rejected" else "plan approval timed out"
        task.failed_stage = self.name()
        return StageResult(
            next_state=TaskState.CANCELLED,
            message=task.error,
            data={"plan_approved": decision or "timeout"},
        )

    async def _assess_clarity(self, task: DevAITask) -> tuple[str, list[str]]:
        """LLM judges intent completeness. No usable LLM → CLEAR (never block
        a run on missing infrastructure)."""
        llm = self.deps.llm
        if llm is None or getattr(llm, "provider_name", "noop") == "noop":
            return "clear", []
        try:
            from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

            detected = task.agent_context.get("detected_tech_stack") or ""
            prompt = (
                "A user asked an autonomous software pipeline to do this:\n\n"
                f"  {task.intent[:800]}\n\n"
                + (f"Repo tech detected so far: {str(detected)[:200]}\n\n" if detected else "")
                + "Reply CLEAR only when the user EXPLICITLY stated the tech stack/"
                "framework, the scope boundaries, AND the core data model. If any "
                "of those are unstated (even if inferable), reply AMBIGUOUS and "
                "make up to 4 CONCRETE PROPOSALS the user can simply confirm — "
                "fill the gaps with your recommended choices, one per line, no "
                "numbering, phrased like: \"We'll build this with Next.js 16 + "
                "FastAPI + PostgreSQL — confirm or name your preferred stack\" or "
                "\"Cart + checkout will be guest-only with mock payments — confirm "
                "or specify auth/payments\". The user should be able to approve "
                "everything with one click."
            )
            response = await llm.generate(
                LLMRequest(
                    messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                    max_tokens=250,
                    temperature=0.0,
                    extra={"agent": "plan_approval"},
                )
            )
            text = (response.text or "").strip()
            if text.upper().startswith("CLEAR"):
                return "clear", []
            questions = [
                line.strip().lstrip("-•* ").strip()
                for line in text.splitlines()[1:]
                if len(line.strip()) > 8
            ][:4]
            return "ambiguous", questions
        except Exception:  # noqa: BLE001 — assessment failure must not block the run
            logger.exception("plan_approval: clarity assessment failed — treating as clear")
            return "clear", []

    def _build_request(self, task: DevAITask, questions: list[str]) -> dict[str, Any]:
        ctx = task.agent_context
        plan = ctx.get("engineering_manager_output") or {}
        plan_summary = ""
        if isinstance(plan, dict):
            plan_summary = str(plan.get("technical_plan") or plan.get("summary") or "")[:1200]
        if not plan_summary:
            # Boardroom runs (and any stage that writes a top-level summary)
            # surface their agreed decision on the approval banner.
            plan_summary = str(ctx.get("plan_summary") or ctx.get("boardroom_decision") or "")[:1200]
        return {
            "gate": self.name(),
            "title": "Plan Approval",
            "kind": "plan_approval",
            "intent": (task.intent or "")[:400],
            "tech_stack": ctx.get("detected_tech_stack"),
            "epic_issue_number": task.epic_issue_number,
            "story_issue_numbers": list(task.story_issue_numbers or []),
            "plan_summary": plan_summary,
            "questions": questions,
            "requested_at": time.time(),
        }

    async def _record_approval_on_epic(self, task: DevAITask, request: dict[str, Any]) -> None:
        """The human just confirmed the proposal — fold it into the epic
        description so the supervised issue is the single source of truth
        for what was approved (tech choices, scope confirmations, plan)."""
        scm = self.deps.scm
        if scm is None or not task.epic_issue_number or getattr(task, "dry_run", False):
            return
        lines = ["## Approved Plan", "", "_Confirmed by the user at the plan-approval gate._", ""]
        if request.get("tech_stack"):
            lines.append(f"**Tech stack:** {str(request['tech_stack'])[:300]}")
        for q in request.get("questions") or []:
            lines.append(f"- [x] {q}")
        if request.get("plan_summary"):
            lines += ["", "### Technical plan", str(request["plan_summary"])[:1500]]
        section = "\n".join(lines)
        try:
            issue = await scm.get_issue(task.repo, task.epic_issue_number)
            body = issue.get("body") or ""
            if "## Approved Plan" not in body:
                await scm.update_issue(task.repo, task.epic_issue_number, body=f"{body}\n\n{section}")
            await scm.add_comment(
                task.repo,
                task.epic_issue_number,
                f"{section}\n\n_Run `{task.id}` is proceeding to implementation._",
            )
        except Exception:  # noqa: BLE001 — supervision is best-effort, never fail the gate
            logger.exception("plan_approval: epic update failed for #%s", task.epic_issue_number)

    async def _await_decision(self, task: DevAITask) -> str | None:
        """Poll the gate decision key, honoring run-control stop. The stage's
        blueprint timeout (set generously in YAML) bounds the wait."""
        sm = self.deps.state_manager
        redis = getattr(sm, "redis", None) if sm is not None else None
        if redis is None:
            return "approved"  # no decision surface (tests) — degrade open
        key = f"devai:pipeline:gate:{task.id}:{self.name()}"
        task.transition(TaskState.AWAITING_APPROVAL)
        getter = getattr(sm, "get_pipeline_control", None)
        while True:
            try:
                decision = await redis.get(key)
            except Exception:  # noqa: BLE001
                logger.exception("plan_approval: decision poll failed")
                decision = None
            if decision:
                return str(decision).lower()
            if getter is not None:
                try:
                    if await getter(task.id) == "stopped":
                        return "rejected"
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self._POLL_SECONDS)


def plan_approval_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _PlanApprovalStage(deps, config)


class _AlmLearnStage(PipelineStage):
    """Distill the finished ALM run into agent memory so future runs benefit.

    Counterpart of the SRE pipeline's `sre_learn` — without it the ALM
    pipeline only ever reads memories that nothing produces. Writes:

      - one EPISODIC record per run: outcome, stages completed/failed, error.
        Episodic records expire (provider-side TTL), so routine run history
        doesn't accumulate forever.
      - one PROCEDURAL record when stages failed mid-run yet the run still
        completed (review/fix loops, or on_failure:continue stages). Which
        stages need iteration on this repo is exactly what the next run
        should recall.

    Dry-runs write nothing. No memory adapter → stage still succeeds,
    just with no learning written (same degrade contract as injection).
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps

    def name(self) -> str:
        return "alm_learn"

    async def execute(self, task: DevAITask) -> StageResult:
        if getattr(task, "dry_run", False):
            return StageResult(
                message="[dry-run] would record run outcome to memory",
                data={"learn_done": True, "dry_run": True},
            )

        if self.deps.memory is None:
            return StageResult(
                message="no memory adapter — nothing learned",
                data={"learn_done": False},
            )

        completed = list(task.stages_completed or [])
        failed = list(task.stages_failed or [])
        outcome = "failed" if (task.error or failed) else "succeeded"
        lines = [
            f"ALM run {outcome}: blueprint={task.blueprint} repo={task.repo or 'n/a'} "
            f"intent={task.intent or task.label or 'n/a'}"
        ]
        if completed:
            lines.append(f"stages completed: {', '.join(completed)}")
        if failed:
            lines.append(f"stages failed: {', '.join(failed)}")
        if task.error:
            lines.append(f"error: {task.error}")
        summary = "\n".join(lines)

        meta = {
            "task_id": task.id,
            "blueprint": task.blueprint,
            "stages_completed": completed,
            "stages_failed": failed,
            "error": task.error or "",
        }

        try:
            await self.deps.memory.remember(
                content=summary,
                agent="alm",
                repo=task.repo or "global",
                memory_type="episodic",
                tags=["alm", "run", task.blueprint, outcome],
                metadata=meta,
            )
            if failed and not task.error:
                # Careful with the claim: on_failure:continue stages land in
                # stages_failed without ever being fixed, so record that these
                # stages needed iteration — not that they were resolved.
                await self.deps.memory.remember(
                    content=(
                        f"On {task.repo or 'this repo'}, stages [{', '.join(failed)}] failed mid-run "
                        f"while the pipeline still completed. Expect these stages to need "
                        f"iteration or manual attention on this repo."
                    ),
                    agent="alm",
                    repo=task.repo or "global",
                    memory_type="procedural",
                    tags=["alm", "flaky-stages", task.blueprint],
                    metadata={"task_id": task.id, "failed_stages": failed},
                )
        except Exception:  # noqa: BLE001
            logger.exception("alm_learn: memory write failed — run completes regardless")
            return StageResult(
                message="memory write failed (see logs)",
                data={"learn_done": False},
            )

        reinforced = await self._reinforce_recalled(task, outcome)
        distilled = await self._distill(task, summary)

        return StageResult(
            message=f"learned via {self.deps.memory.provider_name}: run {outcome}",
            data={
                "learn_done": True,
                "memory_provider": self.deps.memory.provider_name,
                "memories_reinforced": reinforced,
                "memories_distilled": distilled,
            },
        )

    async def _reinforce_recalled(self, task: DevAITask, outcome: str) -> int:
        """Feedback loop: memories the injection stage surfaced into a run
        that SUCCEEDED get a relevance bump — recall that correlates with
        success should rank higher next time."""
        if outcome != "succeeded":
            return 0
        injected = task.agent_context.get("memories") or []
        ids = [m.get("provider_id") for m in injected if isinstance(m, dict) and m.get("provider_id")]
        if not ids:
            return 0
        try:
            return await self.deps.memory.reinforce(ids)
        except Exception:  # noqa: BLE001
            logger.exception("alm_learn: reinforce failed — non-fatal")
            return 0

    async def _distill(self, task: DevAITask, summary: str) -> int:
        """LLM-assisted distillation: one cheap call turning the raw run
        record into standalone facts future runs on this repo should know.
        Raw event blobs are what make recall noisy — this is the signal.

        Requires a real LLM adapter; Noop (or any failure) → 0 records,
        the stage still succeeds.
        """
        llm = self.deps.llm
        if llm is None or getattr(llm, "provider_name", "noop") == "noop":
            return 0

        try:
            from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

            prompt = (
                "You are distilling the outcome of an automated software-delivery "
                "pipeline run into durable knowledge.\n\n"
                f"Run record:\n{summary}\n\n"
                "Write up to 3 standalone facts that would help FUTURE runs on this "
                "repository (conventions discovered, stages that need iteration, "
                "root causes, decisions made). One per line, no numbering, no "
                "preamble. Each must make sense without this run's context. "
                "If nothing is worth remembering, reply exactly NONE."
            )
            response = await llm.generate(
                LLMRequest(
                    messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                    max_tokens=300,
                    temperature=0.2,
                    extra={"agent": "alm_learn"},
                )
            )
            text = (response.text or "").strip()
            if not text or text.upper().startswith("NONE"):
                return 0

            written = 0
            for line in text.splitlines():
                fact = line.strip().lstrip("-•* ").strip()
                if len(fact) < 16:  # skip fragments / stray bullets
                    continue
                await self.deps.memory.remember(
                    fact,
                    agent="alm",
                    repo=task.repo or "global",
                    memory_type="semantic",
                    tags=["alm", "distilled", task.blueprint],
                    metadata={"task_id": task.id, "source": "alm_learn_distill"},
                )
                written += 1
                if written >= 3:
                    break
            return written
        except Exception:  # noqa: BLE001
            logger.exception("alm_learn: distillation failed — non-fatal")
            return 0


def alm_learn_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _AlmLearnStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# post_report — render markdown report + (optionally) post as PR comment
# ──────────────────────────────────────────────────────────────────────


class _PostReportStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.title = config.get("title", "DevAI Pipeline Report")
        self.target = config.get("target", "pr")  # pr | issue | none

    def name(self) -> str:
        return "post_report"

    async def execute(self, task: DevAITask) -> StageResult:
        report = self._render(task)
        # Pull preview URLs off the handover bag so the StageResult.data
        # surfaces them to subscribers (dashboard SSE, task event payload).
        preview_url = str(task.agent_context.get("preview_url") or "")
        editor_url = str(task.agent_context.get("editor_url") or "")
        triggered_by = task.triggered_by or task.agent_context.get("trigger_actor") or ""

        result_data = {
            "report_markdown": report,
            "preview_url": preview_url,
            "editor_url": editor_url,
            "triggered_by": triggered_by,
        }

        if self.target == "none" or self.deps.scm is None:
            return StageResult(message="rendered report (not posted)", data=result_data)

        try:
            if self.target == "pr" and task.pr_number is not None:
                await self.deps.scm.post_pr_comment(task.repo, task.pr_number, report)  # type: ignore[union-attr]
            elif self.target == "issue" and task.issue_number is not None and task.issue_number > 0:
                await self.deps.scm.post_issue_comment(task.repo, task.issue_number, report)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.exception("post_report: failed to post comment")
            return StageResult(message=f"render ok, post failed: {e}", data=result_data)

        return StageResult(message="report posted", data=result_data)

    def _render(self, task: DevAITask) -> str:
        lines = [f"## {self.title}", "", f"**Run:** `{task.id}`", f"**Blueprint:** `{task.blueprint}`"]
        if task.triggered_by:
            lines.append(f"**Triggered by:** `{task.triggered_by}`")
        lines.append("")

        # Preview links — the whole reason post_report exists for the
        # app-scaffold blueprint. Render them at the top so reviewers
        # see them without scrolling past the stage list.
        preview_url = task.agent_context.get("preview_url")
        editor_url = task.agent_context.get("editor_url")
        if preview_url or editor_url:
            lines.append("### Live preview")
            if preview_url:
                lines.append(f"- App: <{preview_url}>")
            if editor_url:
                lines.append(f"- Editor: <{editor_url}>")
            lines.append("")

        if task.stages_completed:
            lines.append("### Stages completed")
            for s in task.stages_completed:
                lines.append(f"- {s}")
        if task.stages_failed:
            lines += ["", "### Stages failed"]
            for s in task.stages_failed:
                lines.append(f"- {s}")
        if task.error:
            lines += ["", f"**Error:** {task.error}"]
        return "\n".join(lines)


def post_report_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _PostReportStage(deps, config)


__all__ = [
    "alm_learn_stage",
    "plan_approval_stage",
    "await_merge_stage",
    "cleanup_stage",
    "context_hydration_stage",
    "create_issue_stage",
    "detect_pr_stage",
    "memory_injection_stage",
    "noop_stage",
    "post_report_stage",
]
