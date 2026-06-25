"""ALM agent stages.

Each factory wraps one existing class from `devai.agents/*` as an `AgentStage`
(the generic dispatcher-backed stage) instead of a bespoke `AgentAdapter`
subclass. The agents themselves are unchanged — `AgentStage` + the SDK's
`LegacyAgent` translate DevAITask ↔ ALMState, run `agent.run()`, and stuff the
patch back into the handover bag, with the per-stage output contracts expressed
as small validator functions.

Long-term these roles become YAML specializations dispatched through the very
same `AgentStage`; the dotted-path bridge here is the interim that drives the 14
existing ALM agents.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.stages._base import run_correlation_label as _run_label
from devai.pipeline.stages.agent_stage import Validator, legacy_agent_stage
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# CI ground truth — stages verify REAL workflow conclusions, independent
# of agent narration. An agent claiming green while github.com/.../actions
# is red is exactly the failure mode this guards against (live incident).
# ──────────────────────────────────────────────────────────────────────


async def _latest_ci_conclusions(deps: StageDeps, repo: str, branch: str) -> tuple[str, str]:
    from devai.services.ci_insight import latest_ci_conclusions

    out = await latest_ci_conclusions(deps.scm, repo, branch)
    return (out["verdict"], out["url"])


async def _failed_job_logs(deps: StageDeps, repo: str, run_url: str) -> str:
    from devai.services.ci_insight import failed_job_logs

    return await failed_job_logs(deps.scm, repo, run_url)


async def _repo_has_workflows(deps: StageDeps, repo: str, branch: str) -> bool:
    from devai.services.ci_insight import repo_has_workflows

    return await repo_has_workflows(deps.scm, repo, branch)


async def _assert_ci_truth(deps: StageDeps, task: Any, patch: dict[str, Any], *, stage: str) -> None:
    """Raise unless the branch's REAL workflows are green.

    Raising here fails the stage visibly, which engages the executor's
    retry → diagnose → fix loop — instead of a red repo sailing to deploy.
    'unknown' (non-GitHub / API blip) keeps the agent's verdict: this guard
    must never block on its own outage, only on observed red builds.
    """
    branch = str(patch.get("branch_name") or task.branch_name or "")
    if not branch or not task.repo:
        return
    verdict, url = await _latest_ci_conclusions(deps, task.repo, branch)
    if verdict == "success" or verdict == "unknown":
        return
    if verdict == "in_progress":
        raise RuntimeError(
            f"{stage}: CI for '{branch}' is still running ({url}) — cannot declare success until it completes"
        )
    if verdict == "none":
        if await _repo_has_workflows(deps, task.repo, branch):
            raise RuntimeError(
                f"{stage}: repo has workflows under .github/workflows but NONE ran for '{branch}' — "
                "CI never triggered (workflow misconfiguration); fix the workflow trigger before proceeding"
            )
        return  # genuinely no CI in this repo — nothing to verify

    # Red build: pull the failed jobs' logs so the diagnose→fix round works
    # from the ACTUAL errors. Lands in agent_context (the re-run agent and
    # the recovery diagnosis read it) and in the error (visible on the run).
    log_excerpt = await _failed_job_logs(deps, task.repo, url)
    ctx = getattr(task, "agent_context", None)
    if log_excerpt and isinstance(ctx, dict):
        ctx["ci_failure_logs"] = log_excerpt
        ctx["ci_failed_branch"] = branch
        ctx["ci_failed_run_url"] = url
    raise RuntimeError(
        f"{stage}: GitHub workflows for '{branch}' concluded '{verdict}' ({url}) — "
        "the stage cannot pass while the repo's actual builds are red. "
        + (f"Failure excerpt:\n{log_excerpt[:800]}" if log_excerpt else "Could not fetch failure logs.")
    )


# ──────────────────────────────────────────────────────────────────────
# Output-contract validators — run after a successful agent result. They
# raise so a no-op 'completed' stage (narration without side effects)
# fails visibly and the executor's retry kicks in. The stub path returns
# before validation, so `patch` is never a stub here.
# ──────────────────────────────────────────────────────────────────────


async def _validate_pull_request(deps: StageDeps, task: DevAITask, patch: dict[str, Any], **_: Any) -> None:
    """Implementation stages MUST produce a pull request — narrative output
    with no commits is a failed implementation, not a success."""
    pr = patch.get("pr_number") or task.pr_number
    if not isinstance(pr, int) or pr <= 0:
        raise RuntimeError(
            "implementation produced no pull request (no commits reached the repo) — "
            f"summary was: {str(patch.get('implementation_summary') or patch.get('summary') or '')[:200]!r}"
        )
    # Correlate the PR to the fleet run + mark it agent-authored (best-effort).
    if deps.scm is not None and not task.dry_run:
        try:
            await deps.scm.add_labels(task.repo, pr, ["devai:pr", _run_label(task.id)])
        except Exception:  # noqa: BLE001
            logger.debug("PR labeling failed for #%s", pr, exc_info=True)


def _require_outputs(required: tuple[str, ...]) -> Validator:
    """Quality-gate output contract: the agent must produce its verdict fields.
    A 0.0s 'completed' review/scan stage with no decision is a silent no-op."""

    async def _validate(
        deps: StageDeps, task: DevAITask, patch: dict[str, Any], *, stage_name: str, output_key: str, **_: Any
    ) -> None:
        if patch.get(f"{output_key}_stub"):
            return
        missing = [k for k in required if patch.get(k) in (None, "")]
        if missing:
            raise RuntimeError(
                f"{stage_name} produced no {'/'.join(missing)} — the agent returned "
                f"narrative output without doing its job (keys present: {sorted(patch.keys())[:8]})"
            )

    return _validate


async def _validate_ci_truth(
    deps: StageDeps, task: DevAITask, patch: dict[str, Any], *, stage_name: str, output_key: str, **_: Any
) -> None:
    if patch.get(f"{output_key}_stub"):
        return
    # Ground truth over narration: whatever the agent reported, the branch's
    # actual workflows must be green for this stage to pass.
    await _assert_ci_truth(deps, task, patch, stage=stage_name)


async def _validate_run_tests(
    deps: StageDeps, task: DevAITask, patch: dict[str, Any], *, stage_name: str, output_key: str, **_: Any
) -> None:
    if patch.get(f"{output_key}_stub"):
        return
    # The QA agent must REPORT results (counts may legitimately be 0 only
    # alongside an explicit summary of what it did).
    if patch.get("test_total") in (None, "") and patch.get("test_passed") in (None, ""):
        raise RuntimeError(
            "run_tests produced no test results — the QA agent must write/run "
            f"tests and report counts (keys present: {sorted(patch.keys())[:8]})"
        )
    # Tests execute through the repo's own CI — so the branch's actual workflows
    # being red means the tests did NOT pass, whatever the narrated counts say.
    await _assert_ci_truth(deps, task, patch, stage=stage_name)


async def _validate_deploy(deps: StageDeps, task: DevAITask, patch: dict[str, Any], **_: Any) -> None:
    """Output contract: a deploy that FAILED must fail the stage. The agent's
    own verdict decides the outcome, so failures surface visibly and the
    recovery agent gets its shot."""
    status = str(patch.get("deploy_status") or "").lower()
    if status in ("failed", "failure", "error"):
        detail = str(patch.get("deploy_error") or patch.get("summary") or "release manager reported failure")[:300]
        raise RuntimeError(f"deploy_release reported deploy_status={status!r}: {detail}")


# ──────────────────────────────────────────────────────────────────────
# Planning chain — ingest → tech → analyze → epic → stories → plan
# ──────────────────────────────────────────────────────────────────────


def ingest_documents_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="ingest_documents",
        dotted="devai.agents.document_analyzer.DocumentAnalyzerAgent",
        output_key="document_analyzer",
        next_state=TaskState.INGESTING,
    )


def detect_tech_stack_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="detect_tech_stack",
        dotted="devai.agents.tech_detector.TechDetectorAgent",
        output_key="tech_detector",
        next_state=TaskState.ANALYZING,
    )


def analyze_requirements_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="analyze_requirements",
        dotted="devai.agents.requirements_analyst.RequirementsAnalystAgent",
        output_key="requirements_analyst",
        next_state=TaskState.ANALYZING,
    )


def create_epic_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="create_epic",
        dotted="devai.agents.product_director.ProductDirectorAgent",
        output_key="product_director",
        next_state=TaskState.PLANNING,
    )


def create_stories_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Runs ProductDirectorAgent (stage-aware dispatch routes on the stage name)
    with the epic context from create_epic's handover — the stories land as
    GitHub issues linked + tracked on the epic."""
    return legacy_agent_stage(
        deps,
        name="create_stories",
        dotted="devai.agents.product_director.ProductDirectorAgent",
        output_key="story_creator",
        next_state=TaskState.PLANNING,
    )


def create_plan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="create_plan",
        dotted="devai.agents.engineering_manager.EngineeringManagerAgent",
        output_key="engineering_manager",
        next_state=TaskState.PLANNING,
    )


# ──────────────────────────────────────────────────────────────────────
# Implementation chain
# ──────────────────────────────────────────────────────────────────────


def implement_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="implement_code",
        dotted="devai.agents.senior_developer.SeniorDeveloperAgent",
        output_key="senior_developer",
        next_state=TaskState.IMPLEMENTING,
        validator=_validate_pull_request,
    )


def db_engineering_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="db_engineering",
        dotted="devai.agents.db_engineer.DBEngineerAgent",
        output_key="db_engineer",
        next_state=TaskState.IMPLEMENTING,
    )


# ──────────────────────────────────────────────────────────────────────
# Quality gates
# ──────────────────────────────────────────────────────────────────────


def review_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="review_code",
        dotted="devai.agents.staff_reviewer.StaffReviewerAgent",
        output_key="staff_reviewer",
        next_state=TaskState.REVIEWING,
        validator=_require_outputs(("review_decision",)),
    )


def staff_review_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Final review gate — same agent as review_code but a distinct stage so a
    blueprint can run a smoke-review early and a staff review late."""
    return legacy_agent_stage(
        deps,
        name="staff_review",
        dotted="devai.agents.staff_reviewer.StaffReviewerAgent",
        output_key="staff_reviewer_final",
        next_state=TaskState.REVIEWING,
    )


def security_scan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="security_scan",
        dotted="devai.agents.security_expert.SecurityExpertAgent",
        output_key="security_expert",
        next_state=TaskState.SECURITY_SCANNING,
        validator=_require_outputs(("security_decision",)),
    )


def monitor_build_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="monitor_build",
        dotted="devai.agents.ci_monitor.CIMonitorAgent",
        output_key="ci_monitor",
        next_state=TaskState.BUILDING,
        validator=_validate_ci_truth,
    )


def run_tests_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="run_tests",
        dotted="devai.agents.qa_tester.QATesterAgent",
        output_key="qa_tester",
        next_state=TaskState.TESTING,
        validator=_validate_run_tests,
    )


# ──────────────────────────────────────────────────────────────────────
# Self-healing test loop — diagnose → bug issue → fix → re-test
#
# Test failures are not a dead end: an analyst pass investigates WHY the
# tests failed, files a GitHub bug issue connected to the epic + stories +
# PR, hands the senior developer a focused fix brief, and the blueprint
# re-runs the QA stage. This is the SDLC loop the LangGraph orchestrator
# had ("run_tests ←→ loop max 2") expressed as a bounded DAG chain.
# ──────────────────────────────────────────────────────────────────────


class _DiagnoseTestFailuresStage(PipelineStage):
    """Root-cause failing tests and file a linked bug issue.

    Reads the QA stage's outputs (test_failed + qa_tester_output), asks the
    LLM for a root-cause + fix brief, then creates a `devai:bug`-labeled
    issue that references the epic, the stories, and the PR — so the bug is
    navigable from everything it affects (and the epic timeline shows it).
    Writes `bug_issue_number` + `test_fix_brief` into the handover bag for
    the fix stage.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return "diagnose_test_failures"

    async def execute(self, task):  # type: ignore[override]
        failed = int(task.agent_context.get("test_failed") or 0)
        if failed <= 0:
            return StageResult(message="no test failures to diagnose", data={"diagnosis_skipped": True})

        qa_out = task.agent_context.get("qa_tester_output") or {}
        qa_summary = str(qa_out)[:2500] if not isinstance(qa_out, str) else qa_out[:2500]

        root_cause, fix_brief = await self._analyze(task, failed, qa_summary)

        bug_number: int | None = None
        bug_url = ""
        if self.deps.scm is not None and not task.dry_run:
            refs = []
            if task.epic_issue_number:
                refs.append(f"**Epic:** #{task.epic_issue_number}")
            if task.story_issue_numbers:
                refs.append("**Related stories:** " + " ".join(f"#{n}" for n in task.story_issue_numbers[:10]))
            if task.pr_number:
                refs.append(f"**Pull request:** #{task.pr_number}")
            body = (
                f"{failed} test(s) failing on run `{task.id}`.\n\n"
                f"## Root cause analysis\n{root_cause}\n\n"
                f"## Proposed fix\n{fix_brief}\n\n" + "\n".join(refs) + "\n\n"
                "_Filed automatically by the QA diagnosis stage; the senior developer "
                "agent will attempt the fix on the existing PR branch and re-run the tests._"
            )
            try:
                issue = await self.deps.scm.create_issue(
                    task.repo,
                    title=f"[bug] {failed} failing test(s) on PR #{task.pr_number or '?'}: {(root_cause or 'test failures')[:80]}",
                    body=body,
                    labels=["bug", "devai:bug", "devai:auto-diagnosed", _run_label(task.id)],
                )
                bug_number = issue.get("number")
                bug_url = issue.get("html_url", "")
            except Exception:  # noqa: BLE001 — diagnosis still useful without the issue
                logger.exception("diagnose_test_failures: bug issue creation failed")

        return StageResult(
            message=f"diagnosed {failed} failing test(s)" + (f" — filed bug #{bug_number}" if bug_number else ""),
            data={
                "bug_issue_number": bug_number,
                "bug_issue_url": bug_url,
                "test_fix_brief": f"{root_cause}\n\n{fix_brief}".strip()[:3000],
            },
        )

    async def _analyze(self, task, failed: int, qa_summary: str) -> tuple[str, str]:
        # One policy: the user's own connector (role-priced) when they have
        # one, the trial-metered platform chain when they don't, None once
        # the trial is exhausted (→ mechanical fallback text below).
        llm = await self.deps.role_llm_for_principal(getattr(task, "triggered_by", "") or "", "utility")
        if llm is None or getattr(llm, "provider_name", "noop") == "noop":
            return (
                f"{failed} test(s) failed — see the QA stage output on the run.",
                "Re-run the failing tests locally, fix the assertions or the regressed code path, and push to the PR branch.",
            )
        try:
            from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

            prompt = (
                f"An automated pipeline run has {failed} failing test(s).\n\n"
                f"QA stage output (truncated):\n{qa_summary}\n\n"
                "Reply in exactly two sections:\nROOT CAUSE: <2-3 sentences>\n"
                "FIX: <concrete instructions for the developer agent — which files/behaviors to change>"
            )
            response = await llm.generate(
                LLMRequest(
                    messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                    max_tokens=500,
                    temperature=0.0,
                    model=str(getattr(self.deps.config, "llm_model_utility", "") or ""),
                    extra={
                        "agent": "qa_diagnosis",
                        "triggered_by": getattr(task, "triggered_by", "") or "",
                        "run_id": getattr(task, "id", "") or "",
                    },
                )
            )
            text = (response.text or "").strip()
            root, _, fix = text.partition("FIX:")
            root = root.replace("ROOT CAUSE:", "").strip() or text[:400]
            return root[:1200], (fix.strip() or "Apply the root-cause fix above.")[:1500]
        except Exception:  # noqa: BLE001
            logger.exception("diagnose_test_failures: LLM analysis failed")
            return (f"{failed} test(s) failed (automated analysis unavailable).", "Investigate the QA output and fix.")


def diagnose_test_failures_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DiagnoseTestFailuresStage(deps, config)


def _fix_test_failures_instruction(task: DevAITask) -> str:
    """The fix brief that overrides task.intent for the fix stage — the senior
    developer applies the diagnosed fix on the EXISTING PR branch."""
    bug = task.agent_context.get("bug_issue_number")
    brief = task.agent_context.get("test_fix_brief") or ""
    return (
        "Tests are failing on the existing pull request"
        + (f" (bug #{bug})" if bug else "")
        + ". Apply the SMALLEST fix that makes them pass — do not redesign or "
        "re-implement features.\n\n"
        f"## Diagnosis\n{brief}\n\n"
        "Work on the EXISTING branch/PR; commit the fix and note what changed."
    )


def fix_test_failures_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Senior developer applies the diagnosed fix on the existing PR branch."""
    return legacy_agent_stage(
        deps,
        name="fix_test_failures",
        dotted="devai.agents.senior_developer.SeniorDeveloperAgent",
        output_key="senior_developer",
        next_state=TaskState.IMPLEMENTING,
        validator=_validate_pull_request,
        instruction_builder=_fix_test_failures_instruction,
        extra_data={"test_fix_applied": True},
    )


# ──────────────────────────────────────────────────────────────────────
# Deployment chain
# ──────────────────────────────────────────────────────────────────────


def provision_infra_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="provision_infra",
        dotted="devai.agents.infra_provisioner.InfraProvisionerAgent",
        output_key="infra_provisioner",
        next_state=TaskState.PROVISIONING,
    )


def deploy_release_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return legacy_agent_stage(
        deps,
        name="deploy_release",
        dotted="devai.agents.release_manager.ReleaseManagerAgent",
        output_key="release_manager",
        next_state=TaskState.DEPLOYING,
        validator=_validate_deploy,
    )


__all__ = [
    "analyze_requirements_stage",
    "create_epic_stage",
    "create_plan_stage",
    "create_stories_stage",
    "db_engineering_stage",
    "deploy_release_stage",
    "detect_tech_stack_stage",
    "diagnose_test_failures_stage",
    "fix_test_failures_stage",
    "implement_code_stage",
    "ingest_documents_stage",
    "monitor_build_stage",
    "provision_infra_stage",
    "review_code_stage",
    "run_tests_stage",
    "security_scan_stage",
    "staff_review_stage",
]
