"""Deterministic lifecycle stages — no agent calls.

These are the Fiber `lifecycle` family: create_issue, detect_pr, cleanup,
post_report, plus a couple of context-shaping stages (context_hydration,
memory_injection) and a noop for blueprint testing.

Each function in this module is a stage factory. The function returns a
PipelineStage instance configured with the StageDeps + per-stage config.
"""

from __future__ import annotations

import logging

from devai.pipeline.interfaces import PipelineStage, StageDeps
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
                labels=[self.deps.config.pipeline_label] if hasattr(self.deps.config, "pipeline_label") else None,
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
        # In the current architecture there's no sandbox pod to tear down;
        # this is a hook for future sandbox/preview support.
        task.sandbox_pod = None
        task.dev_server_port = None
        return StageResult(message="cleanup complete", data={"cleanup_done": True})


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
    "await_merge_stage",
    "cleanup_stage",
    "context_hydration_stage",
    "create_issue_stage",
    "detect_pr_stage",
    "memory_injection_stage",
    "noop_stage",
    "post_report_stage",
]
