"""ALM agent adapter stages.

Each factory in this module wraps one existing class from `devai.agents/*`
behind the PipelineStage interface. The agents themselves are unchanged —
this layer just translates DevAITask ↔ ALMState, runs `agent.run()`, and
stuffs the patch back into the handover bag.

Long-term these go away in favor of YAML specializations + LLM provider
dispatch, but for now this is how the blueprint runtime drives the 14
existing ALM agents.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.stages._base import AgentAdapter, _safe_agent
from devai.pipeline.stages._base import run_correlation_label as _run_label
from devai.pipeline.types import TaskState

logger = logging.getLogger(__name__)


def _make(klass: Any, deps: StageDeps) -> Any | None:
    return _safe_agent(klass, deps)


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
# Planning chain — ingest → tech → analyze → epic → stories → plan
# ──────────────────────────────────────────────────────────────────────


class _IngestDocumentsStage(AgentAdapter):
    def name(self) -> str:
        return "ingest_documents"

    def role_key(self) -> str:
        return "document_analyzer"

    def _next_state(self) -> TaskState:
        return TaskState.INGESTING

    def _make_agent(self):
        from devai.agents.document_analyzer import DocumentAnalyzerAgent

        return _make(DocumentAnalyzerAgent, self.deps)


def ingest_documents_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _IngestDocumentsStage(deps, config)


class _DetectTechStackStage(AgentAdapter):
    def name(self) -> str:
        return "detect_tech_stack"

    def role_key(self) -> str:
        return "tech_detector"

    def _next_state(self) -> TaskState:
        return TaskState.ANALYZING

    def _make_agent(self):
        from devai.agents.tech_detector import TechDetectorAgent

        return _make(TechDetectorAgent, self.deps)


def detect_tech_stack_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DetectTechStackStage(deps, config)


class _AnalyzeRequirementsStage(AgentAdapter):
    def name(self) -> str:
        return "analyze_requirements"

    def role_key(self) -> str:
        return "requirements_analyst"

    def _next_state(self) -> TaskState:
        return TaskState.ANALYZING

    def _make_agent(self):
        from devai.agents.requirements_analyst import RequirementsAnalystAgent

        return _make(RequirementsAnalystAgent, self.deps)


def analyze_requirements_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _AnalyzeRequirementsStage(deps, config)


class _CreateEpicStage(AgentAdapter):
    def name(self) -> str:
        return "create_epic"

    def role_key(self) -> str:
        return "product_director"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        from devai.agents.product_director import ProductDirectorAgent

        return _make(ProductDirectorAgent, self.deps)


def create_epic_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreateEpicStage(deps, config)


class _CreateStoriesStage(AgentAdapter):
    """Runs ProductDirectorAgent.run_stories (stage-aware dispatch routes on
    the stage name) with the epic context from create_epic's handover — the
    stories land as GitHub issues linked + tracked on the epic."""

    def name(self) -> str:
        return "create_stories"

    def role_key(self) -> str:
        return "story_creator"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        from devai.agents.product_director import ProductDirectorAgent

        return _make(ProductDirectorAgent, self.deps)


def create_stories_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreateStoriesStage(deps, config)


class _CreatePlanStage(AgentAdapter):
    def name(self) -> str:
        return "create_plan"

    def role_key(self) -> str:
        return "engineering_manager"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        from devai.agents.engineering_manager import EngineeringManagerAgent

        return _make(EngineeringManagerAgent, self.deps)


def create_plan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreatePlanStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Implementation chain
# ──────────────────────────────────────────────────────────────────────


class _ImplementCodeStage(AgentAdapter):
    def name(self) -> str:
        return "implement_code"

    async def _post_validate(self, task, patch) -> None:
        await _require_pull_request(self, task, patch)

    def role_key(self) -> str:
        return "senior_developer"

    def _next_state(self) -> TaskState:
        return TaskState.IMPLEMENTING

    def _make_agent(self):
        from devai.agents.senior_developer import SeniorDeveloperAgent

        return _make(SeniorDeveloperAgent, self.deps)


def implement_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ImplementCodeStage(deps, config)


async def _require_outputs(stage: AgentAdapter, patch: dict, required: tuple[str, ...]) -> None:
    """Quality-gate output contract: the agent must produce its verdict
    fields. A 0.0s 'completed' review/scan/test stage with no decision is a
    silent no-op, not a success — raise so the retry kicks in and persistent
    emptiness fails visibly."""
    if patch.get(f"{stage.role_key()}_stub"):
        return  # stub path already announces itself
    missing = [k for k in required if patch.get(k) in (None, "")]
    if missing:
        raise RuntimeError(
            f"{stage.name()} produced no {'/'.join(missing)} — the agent returned "
            f"narrative output without doing its job (keys present: {sorted(patch.keys())[:8]})"
        )


async def _require_pull_request(stage: AgentAdapter, task, patch) -> None:
    """Implementation stages MUST produce a pull request — narrative output
    with no commits is a failed implementation, not a success."""
    pr = patch.get("pr_number") or task.pr_number
    if not isinstance(pr, int) or pr <= 0:
        raise RuntimeError(
            "implementation produced no pull request (no commits reached the repo) — "
            f"summary was: {str(patch.get('implementation_summary') or patch.get('summary') or '')[:200]!r}"
        )
    # Correlate the PR to the fleet run + mark it agent-authored (best-effort).
    if stage.deps.scm is not None and not task.dry_run:
        from devai.pipeline.stages._base import run_correlation_label

        try:
            await stage.deps.scm.add_labels(task.repo, pr, ["devai:pr", run_correlation_label(task.id)])
        except Exception:  # noqa: BLE001
            logger.debug("PR labeling failed for #%s", pr, exc_info=True)


class _DBEngineeringStage(AgentAdapter):
    def name(self) -> str:
        return "db_engineering"

    def role_key(self) -> str:
        return "db_engineer"

    def _next_state(self) -> TaskState:
        return TaskState.IMPLEMENTING

    def _make_agent(self):
        from devai.agents.db_engineer import DBEngineerAgent

        return _make(DBEngineerAgent, self.deps)


def db_engineering_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DBEngineeringStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Quality gates
# ──────────────────────────────────────────────────────────────────────


class _ReviewCodeStage(AgentAdapter):
    def name(self) -> str:
        return "review_code"

    async def _post_validate(self, task, patch) -> None:
        await _require_outputs(self, patch, ("review_decision",))

    def role_key(self) -> str:
        return "staff_reviewer"

    def _next_state(self) -> TaskState:
        return TaskState.REVIEWING

    def _make_agent(self):
        from devai.agents.staff_reviewer import StaffReviewerAgent

        return _make(StaffReviewerAgent, self.deps)


def review_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ReviewCodeStage(deps, config)


class _StaffReviewStage(AgentAdapter):
    """Final review gate — same agent as review_code but distinct stage so
    a blueprint can run a smoke-review early and a staff review late."""

    def name(self) -> str:
        return "staff_review"

    def role_key(self) -> str:
        return "staff_reviewer_final"

    def _next_state(self) -> TaskState:
        return TaskState.REVIEWING

    def _make_agent(self):
        from devai.agents.staff_reviewer import StaffReviewerAgent

        return _make(StaffReviewerAgent, self.deps)


def staff_review_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _StaffReviewStage(deps, config)


class _SecurityScanStage(AgentAdapter):
    def name(self) -> str:
        return "security_scan"

    async def _post_validate(self, task, patch) -> None:
        await _require_outputs(self, patch, ("security_decision",))

    def role_key(self) -> str:
        return "security_expert"

    def _next_state(self) -> TaskState:
        return TaskState.SECURITY_SCANNING

    def _make_agent(self):
        from devai.agents.security_expert import SecurityExpertAgent

        return _make(SecurityExpertAgent, self.deps)


def security_scan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _SecurityScanStage(deps, config)


class _MonitorBuildStage(AgentAdapter):
    def name(self) -> str:
        return "monitor_build"

    def role_key(self) -> str:
        return "ci_monitor"

    def _next_state(self) -> TaskState:
        return TaskState.BUILDING

    async def _post_validate(self, task, patch) -> None:
        # Ground truth over narration: whatever the agent reported, the
        # branch's actual workflows must be green for this stage to pass.
        if patch.get(f"{self.role_key()}_stub"):
            return
        await _assert_ci_truth(self.deps, task, patch, stage="monitor_build")

    def _make_agent(self):
        from devai.agents.ci_monitor import CIMonitorAgent

        return _make(CIMonitorAgent, self.deps)


def monitor_build_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _MonitorBuildStage(deps, config)


class _RunTestsStage(AgentAdapter):
    def name(self) -> str:
        return "run_tests"

    async def _post_validate(self, task, patch) -> None:
        # The QA agent must REPORT results (counts may legitimately be 0
        # only alongside an explicit summary of what it did).
        if patch.get(f"{self.role_key()}_stub"):
            return
        if patch.get("test_total") in (None, "") and patch.get("test_passed") in (None, ""):
            raise RuntimeError(
                "run_tests produced no test results — the QA agent must write/run "
                f"tests and report counts (keys present: {sorted(patch.keys())[:8]})"
            )
        # Tests execute through the repo's own CI — so the branch's actual
        # workflows being red means the tests did NOT pass, whatever the
        # narrated counts say.
        await _assert_ci_truth(self.deps, task, patch, stage="run_tests")

    def role_key(self) -> str:
        return "qa_tester"

    def _next_state(self) -> TaskState:
        return TaskState.TESTING

    def _make_agent(self):
        from devai.agents.qa_tester import QATesterAgent

        return _make(QATesterAgent, self.deps)


def run_tests_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _RunTestsStage(deps, config)


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
        from devai.pipeline.types import StageResult

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
            message=f"diagnosed {failed} failing test(s)"
            + (f" — filed bug #{bug_number}" if bug_number else ""),
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
        llm = await self.deps.role_llm_for_principal(
            getattr(task, "triggered_by", "") or "", "utility"
        )
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


class _FixTestFailuresStage(_ImplementCodeStage):
    """Senior developer applies the diagnosed fix on the existing PR branch."""

    def name(self) -> str:
        return "fix_test_failures"

    def _build_state(self, task) -> dict[str, Any]:  # type: ignore[override]
        state = super()._build_state(task)
        bug = task.agent_context.get("bug_issue_number")
        brief = task.agent_context.get("test_fix_brief") or ""
        state["requirements"] = (
            "Tests are failing on the existing pull request"
            + (f" (bug #{bug})" if bug else "")
            + ". Apply the SMALLEST fix that makes them pass — do not redesign or "
            "re-implement features.\n\n"
            f"## Diagnosis\n{brief}\n\n"
            "Work on the EXISTING branch/PR; commit the fix and note what changed."
        )
        return state

    def _build_result(self, task, patch):  # type: ignore[override]
        result = super()._build_result(task, patch)
        result.data["test_fix_applied"] = True
        return result

    def _next_state(self) -> TaskState:
        return TaskState.IMPLEMENTING


def fix_test_failures_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _FixTestFailuresStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Deployment chain
# ──────────────────────────────────────────────────────────────────────


class _ProvisionInfraStage(AgentAdapter):
    def name(self) -> str:
        return "provision_infra"

    def role_key(self) -> str:
        return "infra_provisioner"

    def _next_state(self) -> TaskState:
        return TaskState.PROVISIONING

    def _make_agent(self):
        from devai.agents.infra_provisioner import InfraProvisionerAgent

        return _make(InfraProvisionerAgent, self.deps)


def provision_infra_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ProvisionInfraStage(deps, config)


class _DeployReleaseStage(AgentAdapter):
    def name(self) -> str:
        return "deploy_release"

    def role_key(self) -> str:
        return "release_manager"

    def _next_state(self) -> TaskState:
        return TaskState.DEPLOYING

    def _make_agent(self):
        from devai.agents.release_manager import ReleaseManagerAgent

        return _make(ReleaseManagerAgent, self.deps)

    async def _post_validate(self, task, patch) -> None:
        """Output contract: a deploy that FAILED must fail the stage.

        A live run 'completed' deploy-release in 5.1s with
        deploy_status='failed' — the milestone comment said ✅ and the run
        finished green. The same truth rule as review/security/tests: the
        agent's own verdict decides the stage outcome, so failures surface
        visibly and the recovery agent gets its shot."""
        status = str(patch.get("deploy_status") or "").lower()
        if status in ("failed", "failure", "error"):
            detail = str(
                patch.get("deploy_error") or patch.get("summary") or "release manager reported failure"
            )[:300]
            raise RuntimeError(f"deploy_release reported deploy_status={status!r}: {detail}")


def deploy_release_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DeployReleaseStage(deps, config)


__all__ = [
    "diagnose_test_failures_stage",
    "fix_test_failures_stage",
    "analyze_requirements_stage",
    "create_epic_stage",
    "create_plan_stage",
    "create_stories_stage",
    "db_engineering_stage",
    "deploy_release_stage",
    "detect_tech_stack_stage",
    "implement_code_stage",
    "ingest_documents_stage",
    "monitor_build_stage",
    "provision_infra_stage",
    "review_code_stage",
    "run_tests_stage",
    "security_scan_stage",
    "staff_review_stage",
]
