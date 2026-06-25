"""CI Engineer Agent — owns the entire CI workflow lifecycle end-to-end.

Four responsibilities, in order:

1. **Bootstrap the workflow** if the repo doesn't have one yet for the
   detected stack. Renders ``.github/workflows/ci.yml`` from the active
   SkillProfile's template and commits it to the branch.
2. **Manage repo visibility** for the public→build→private cycle that
   tesserix private repos require because of limited Actions minutes.
3. **Monitor the build** to completion.
4. **Fix CI issues autonomously**. On failure, the agent uses Claude
   with GitHub tools to read the failed logs + the workflow file,
   classify the failure (workflow misconfiguration vs application
   code), and either:
     - **Fix workflow issues itself** (bump action versions, fix Node
       version, add missing env vars, fix path filters, swap deprecated
       runners) and commit the fix to the branch — then waits for the
       new build, or
     - **Escalate code issues to the Senior Developer** with a detailed
       diagnosis pulled from the actual logs.

That second pass is what makes it a real CI engineer rather than a
passive monitor — it doesn't punt every failure back to the dev agent.

Uses Claude for autonomous fixing (with tools) and OpenAI for the
fast first-pass diagnosis.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from devai.agents.skills import get_skill_profile
from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# Max time to wait for a build (15 minutes)
BUILD_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 15
# Max time to wait for ALL builds to drain before making private
DRAIN_TIMEOUT_SECONDS = 1200  # 20 minutes
# Maximum number of autonomous CI fix attempts per pipeline run before
# we give up and escalate to the Senior Developer
MAX_CI_FIX_ATTEMPTS = 2

# Subset of GitHub tools the CI Engineer needs to read + edit workflow
# files. Deliberately excludes tools that touch application source files
# — the CI agent only modifies .github/workflows/, not application code.
CI_FIX_TOOLS = [
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_commit_file",
        "github_ci_status",
        "github_ci_failure_logs",
        "github_rerun_failed_jobs",
        "github_add_comment",
    }
]

CI_FIX_SYSTEM_PROMPT = """You are a Senior CI/CD Engineer fixing a failing GitHub Actions build.

You have full read/write access to the repo's `.github/workflows/` directory
via the github_* tools. You DO NOT touch application source files — your
job is to fix the CI workflow itself when the failure is a workflow problem.

## Your decision tree

1. Read the failed job names + failed step names + the actual workflow YAML
   for the failing workflow. Use `github_get_file_content` on every
   `.yml` / `.yaml` file under `.github/workflows/`.
2. Classify the root cause as ONE of:
   - **WORKFLOW** — wrong action version, deprecated runner image, wrong
     Node/Python/Go version, missing `permissions:`, missing env var that
     should come from secrets, wrong working-directory, broken matrix,
     bad cache key, missing `setup-*` step, deprecated action.
   - **CODE** — failing tests, type errors, lint errors in the application
     source, missing dependency in `package.json` / `go.mod` / `pyproject.toml`,
     compilation errors.
   - **INFRA** — runner out of disk, transient network failure, GHCR
     login failed because token is invalid, GitHub Actions outage.

3. Based on the classification:
   - **WORKFLOW** → fix it. Make the smallest possible edit. Commit the
     change with a descriptive message like "ci: bump actions/checkout to v4".
     Then explain what you changed.
   - **CODE** → DO NOT touch the workflow. Output a JSON object with
     `decision="escalate"` and a focused fix prompt for the developer.
   - **INFRA** → output `decision="retry"` (the orchestrator will retry).

## Output format (always include at the end of your response)

```json
{
  "decision": "fixed" | "escalate" | "retry",
  "classification": "workflow" | "code" | "infra",
  "summary": "one-line description of what you found",
  "files_modified": ["path/to/file1.yml"],
  "developer_prompt": "if escalating, the focused fix prompt for the dev"
}
```

## Hard rules

- NEVER edit application source code (anything outside `.github/workflows/`).
- NEVER modify the workflow's trigger conditions to silence failures.
- NEVER skip or `continue-on-error` a failing job to make it pass.
- ALWAYS use `github_commit_file` with the branch from the user message,
  not the default branch.
- ALWAYS prefer the smallest possible diff.
- After committing a fix, your job is done — return the JSON. The
  orchestrator will re-run the build."""


class CIMonitorAgent(BaseAgent):
    """Monitors GitHub Actions CI builds with private repo visibility management."""

    name = "ci_monitor"
    subscribe_subject = "devai.pipeline.review_complete"
    publish_subject = "devai.pipeline.build_complete"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Build (if missing), monitor, and post-process the CI workflow.

        Handles the public→build→private cycle for tesserix repos with
        limited Actions minutes on private repos.
        """
        repo = state.get("repo_full_name", "")
        branch = state.get("branch_name")

        if not branch:
            a2a.escalate(
                "engineering_manager",
                "No Branch Found",
                "CI Monitor cannot proceed — no branch name in pipeline state.",
            )
            return {
                "build_status": "failure",
                "build_logs": "No branch found in pipeline state",
            }

        # Step 0: Ensure a CI workflow exists for the active skill profile.
        # If the repo has no .github/workflows/ files we render the
        # profile's ci_workflow_template and commit it to the branch.
        await self._ensure_ci_workflow_exists(repo, branch, state, a2a)

        # Step 1: Make repo public for CI (private repos have limited minutes)
        original_visibility = await self._make_public_for_ci(repo, a2a)

        try:
            # Step 2: Poll for the latest workflow run on this branch.
            # FIX-UNTIL-GREEN: when the CI agent pushes a workflow fix, the
            # push triggers a NEW build — wait for it and re-evaluate instead
            # of returning the stale failure. Bounded by MAX_CI_FIX_ATTEMPTS;
            # the loop ends on the first green build, an unfixable diagnosis,
            # or exhausted attempts (then the failure stands, honestly).
            seen_run_ids: set[int] = set()
            run_data = await self._wait_for_build(repo, branch)

            if not run_data:
                a2a.notify(
                    "senior_developer",
                    "No CI Workflow Found",
                    f"No GitHub Actions workflow found for branch '{branch}'. Proceeding without CI validation.",
                )
                return {
                    "build_status": "success",
                    "build_url": "",
                    "build_logs": "No workflow found — skipped CI check",
                }

            fix_attempts = state.get("ci_fix_attempts", 0)
            result: dict[str, Any] = {}
            while True:
                build_run_id = run_data.get("id", 0)
                seen_run_ids.add(int(build_run_id or 0))
                status = run_data.get("conclusion", "unknown")
                url = run_data.get("html_url", "")
                result = {
                    "build_run_id": build_run_id,
                    "build_status": status,
                    "build_url": url,
                    "ci_fix_attempts": fix_attempts,
                }

                if status == "success":
                    a2a.notify(
                        "qa_tester",
                        "CI Build Passed",
                        f"Build #{build_run_id} passed for branch '{branch}'.\nURL: {url}",
                    )
                    a2a.broadcast(
                        "Build Passed",
                        f"CI build #{build_run_id} passed on branch '{branch}'"
                        + (f" after {fix_attempts} CI fix(es)" if fix_attempts else "")
                        + ".",
                        exclude=["qa_tester"],
                    )
                    return result

                # Fetch and analyze failed job logs
                failed_jobs = await self._get_failed_jobs(repo, build_run_id)
                result["failed_jobs"] = failed_jobs

                # Use Groq for a fast first-pass diagnosis
                log_summary = await self._analyze_failure(failed_jobs)
                result["build_logs"] = log_summary

                # Hand off to Claude with workflow-edit tools to attempt an
                # autonomous fix, then WAIT FOR THE RE-TRIGGERED BUILD and
                # re-evaluate — "fix until green", bounded by attempts. The
                # old flow returned 'ci_fix_committed' after pushing a fix
                # without ever checking whether the fix actually worked.
                fix_outcome: dict[str, Any] | None = None
                if fix_attempts < MAX_CI_FIX_ATTEMPTS:
                    fix_outcome = await self._attempt_workflow_fix(
                        repo=repo,
                        branch=branch,
                        build_run_id=build_run_id,
                        failed_jobs=failed_jobs,
                        log_summary=log_summary,
                        state=state,
                        a2a=a2a,
                    )
                    fix_attempts += 1
                    result["ci_fix_attempts"] = fix_attempts
                    if fix_outcome:
                        result["ci_fix_decision"] = fix_outcome.get("decision")
                        result["ci_fix_summary"] = fix_outcome.get("summary")

                if fix_outcome and fix_outcome.get("decision") == "fixed":
                    a2a.notify(
                        "senior_developer",
                        "CI Workflow Fixed by CI Agent",
                        f"CI Engineer fixed: {fix_outcome.get('summary', 'workflow change')}\n"
                        f"Files: {', '.join(fix_outcome.get('files_modified', []))}\n"
                        "Waiting for the re-triggered build to verify the fix.",
                    )
                    new_run = await self._wait_for_new_build(repo, branch, seen_run_ids)
                    if new_run is not None:
                        run_data = new_run
                        continue  # re-evaluate the NEW build
                    # New build never appeared — report honestly, not green.
                    result["build_status"] = "ci_fix_committed"
                    result["build_logs"] = (
                        f"{log_summary}\n\nCI fix committed but no new workflow run appeared "
                        "within the wait budget — verify the workflow triggers on push."
                    )
                    return result

                # Unfixable by the CI agent (or attempts exhausted) — escalate
                # to the developer with the focused prompt the CI agent built.
                escalation_body = (fix_outcome.get("developer_prompt") if fix_outcome else log_summary) or log_summary
                a2a.escalate(
                    "senior_developer",
                    "CI Build Failed",
                    f"Build #{build_run_id} failed on branch '{branch}'"
                    + (f" (after {fix_attempts} CI fix attempt(s))" if fix_attempts else "")
                    + f".\n\n## Diagnosis from CI Engineer\n{escalation_body}\n\nURL: {url}",
                    payload={"failed_jobs": failed_jobs, "ci_fix_outcome": fix_outcome},
                )
                a2a.notify(
                    "staff_reviewer",
                    "CI Build Failed",
                    f"Build failed for the reviewed PR. See: {url}",
                )
                return result

        finally:
            # Step 3: Wait for ALL builds to drain, then make repo private again
            if original_visibility == "private":
                await self._make_private_after_ci(repo, a2a)

    async def _make_public_for_ci(self, repo: str, a2a: A2ABus) -> str:
        """Make repo public for CI builds if it's currently private.

        Returns the original visibility so we can restore it after.
        """
        try:
            original = await self.scm.get_repo_visibility(repo)
            if original == "private":
                logger.info("Making %s public for CI (limited Actions minutes on private repos)", repo)
                await self.scm.set_repo_visibility(repo, "public")
                a2a.notify(
                    "orchestrator",
                    "Repo Made Public for CI",
                    f"Temporarily set {repo} to public for CI build. Will revert to private after ALL builds complete.",
                )
            return original
        except Exception as e:
            logger.warning("Failed to check/set repo visibility: %s", e)
            return "unknown"

    async def _make_private_after_ci(self, repo: str, a2a: A2ABus) -> None:
        """Wait for ALL queued/in-progress builds to finish, then make repo private.

        Never makes private while builds are still running.
        """
        logger.info("Waiting for ALL builds on %s to complete before making private...", repo)
        elapsed = 0

        while elapsed < DRAIN_TIMEOUT_SECONDS:
            try:
                active_runs = await self.scm.get_all_workflow_runs(repo)
                if not active_runs:
                    logger.info("All builds complete on %s — making private", repo)
                    await self.scm.set_repo_visibility(repo, "private")
                    a2a.notify(
                        "orchestrator",
                        "Repo Made Private",
                        f"All CI builds on {repo} completed. Repo set back to private.",
                    )
                    return

                run_ids = [r.get("id", "?") for r in active_runs]
                logger.debug(
                    "Waiting for %d active builds on %s: %s",
                    len(active_runs),
                    repo,
                    run_ids,
                )
            except Exception as e:
                logger.warning("Failed to check active builds: %s", e)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        # Timeout — make private anyway but warn
        logger.warning(
            "Timed out waiting for builds on %s after %ds — making private anyway",
            repo,
            DRAIN_TIMEOUT_SECONDS,
        )
        try:
            await self.scm.set_repo_visibility(repo, "private")
        except Exception as e:
            logger.error("CRITICAL: Failed to make %s private after timeout: %s", repo, e)

        a2a.escalate(
            "supervisor",
            "Build Drain Timeout",
            f"Timed out waiting for all builds on {repo} to complete after {DRAIN_TIMEOUT_SECONDS}s. "
            "Repo was made private but some builds may still be queued. Please verify manually.",
        )

    async def _wait_for_build(self, repo: str, branch: str) -> dict[str, Any] | None:
        """Poll GitHub Actions for the latest workflow run on the branch."""
        elapsed = 0

        while elapsed < BUILD_TIMEOUT_SECONDS:
            try:
                resp = await self.scm._request(
                    "GET",
                    f"/repos/{repo}/actions/runs",
                    params={"branch": branch, "per_page": "1"},
                )
                runs = resp.json().get("workflow_runs", [])

                if runs:
                    run = runs[0]
                    run_status = run.get("status", "")

                    if run_status == "completed":
                        return run

                    logger.debug(
                        "Build %d status: %s (waiting...)",
                        run.get("id", 0),
                        run_status,
                    )

            except Exception as e:
                logger.warning("Failed to check build status: %s", e)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        logger.warning("Build timeout after %ds for branch %s", BUILD_TIMEOUT_SECONDS, branch)
        return None

    async def _wait_for_new_build(self, repo: str, branch: str, seen_run_ids: set[int]) -> dict[str, Any] | None:
        """Wait for a workflow run we have NOT evaluated yet (the build the
        CI agent's fix push re-triggered) to complete. Returns None when no
        new run appears/completes inside the budget — the fix-until-green
        loop then reports honestly instead of assuming the fix worked."""
        elapsed = 0
        while elapsed < BUILD_TIMEOUT_SECONDS:
            try:
                resp = await self.scm._request(
                    "GET",
                    f"/repos/{repo}/actions/runs",
                    params={"branch": branch, "per_page": "5"},
                )
                runs = resp.json().get("workflow_runs", [])
                fresh = [r for r in runs if int(r.get("id", 0) or 0) not in seen_run_ids]
                completed = [r for r in fresh if r.get("status") == "completed"]
                if completed:
                    return completed[0]
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to check for re-triggered build: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
        logger.warning("No new build appeared within %ds for branch %s", BUILD_TIMEOUT_SECONDS, branch)
        return None

    async def _get_failed_jobs(self, repo: str, run_id: int) -> list[dict[str, str]]:
        """Get details of failed jobs in a workflow run."""
        try:
            resp = await self.scm._request(
                "GET",
                f"/repos/{repo}/actions/runs/{run_id}/jobs",
            )
            jobs = resp.json().get("jobs", [])

            failed = []
            for job in jobs:
                if job.get("conclusion") == "failure":
                    failed_steps = [
                        step["name"] for step in job.get("steps", []) if step.get("conclusion") == "failure"
                    ]
                    failed.append(
                        {
                            "name": job.get("name", "unknown"),
                            "failed_steps": ", ".join(failed_steps),
                            "url": job.get("html_url", ""),
                        }
                    )
            return failed

        except Exception as e:
            logger.error("Failed to get job details: %s", e)
            return [{"name": "unknown", "failed_steps": str(e), "url": ""}]

    async def _analyze_failure(self, failed_jobs: list[dict[str, str]]) -> str:
        """Use Groq to analyze build failures and suggest fixes."""
        if not failed_jobs:
            return "No failed jobs found"

        try:
            openai = OpenAIProvider(self.config)

            job_details = "\n".join(f"- Job: {j['name']}, Failed Steps: {j['failed_steps']}" for j in failed_jobs)

            response = await openai.generate(
                prompt=f"Analyze these CI build failures and suggest fixes:\n\n{job_details}",
                system="You are a CI/CD expert. Analyze build failures concisely. "
                "Identify root causes and suggest specific fixes. Be brief.",
                max_tokens=1024,
            )
            return response

        except Exception as e:
            logger.error("Failed to analyze build failure: %s", e)
            return f"Failed jobs: {', '.join(j['name'] for j in failed_jobs)}"

    # ------------------------------------------------------------------
    # Autonomous CI Workflow Fixer
    # ------------------------------------------------------------------

    async def _attempt_workflow_fix(
        self,
        repo: str,
        branch: str,
        build_run_id: int,
        failed_jobs: list[dict[str, str]],
        log_summary: str,
        state: ALMState,
        a2a: A2ABus,
    ) -> dict[str, Any] | None:
        """Run a Claude tool-loop to diagnose + fix a failing build.

        Returns the parsed decision dict, or None on hard failure of the
        loop itself. The decision can be:

        - ``fixed``    — agent committed a workflow change; build will re-run
        - ``escalate`` — code-level issue; pass to Senior Developer
        - ``retry``    — transient infra failure; orchestrator should retry
        """
        try:
            claude = ClaudeProvider(self.config, model=getattr(self.config, "llm_model_review", None))
        except Exception as e:
            logger.warning("Cannot init Claude for CI fix: %s — escalating", e)
            return None

        github_tools = GitHubToolExecutor(self.github)
        profile = get_skill_profile(state.get("skill_profile_name"))

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            return await github_tools.execute(tool_name, tool_input)

        failed_summary = (
            "\n".join(
                f"- Job `{j.get('name', '?')}` — failed steps: {j.get('failed_steps', '?')}\n  URL: {j.get('url', '')}"
                for j in failed_jobs
            )
            or "(no failed jobs returned by GitHub API)"
        )

        user_message = f"""Repository: {repo}
Branch: {branch}
Build run: {build_run_id}
Active stack profile: {profile.display_name}

{profile.render_for_planner()}

## What failed
{failed_summary}

## First-pass log analysis (from a fast model)
{log_summary}

## Your task
1. Use `github_get_repo_tree` to find every workflow file under `.github/workflows/`.
2. Read each one with `github_get_file_content` (use ref="{branch}" to get the
   branch version, not main).
3. Diagnose the root cause and classify it as workflow / code / infra.
4. If WORKFLOW: edit the right file with `github_commit_file` (also pass
   ref="{branch}"). Make the smallest possible change. Use a clear commit
   message starting with "ci:".
5. Output the JSON decision block at the end of your final message.

Remember: NEVER touch application source files. Only `.github/workflows/`.
"""

        try:
            result_text = await claude.run_agent_loop(
                system_prompt=CI_FIX_SYSTEM_PROMPT,
                user_message=user_message,
                tools=CI_FIX_TOOLS,
                tool_executor=tool_executor,
                max_iterations=self.config.claude_max_iterations_review,
            )
        except Exception as e:
            logger.warning("CI fix tool-loop failed: %s — escalating to dev", e)
            return None

        decision = self._parse_ci_fix_decision(result_text)
        logger.info(
            "CI fix decision for build %d on %s@%s: %s (%s)",
            build_run_id,
            repo,
            branch,
            decision.get("decision"),
            decision.get("classification"),
        )

        # Post a transparency comment on the PR so humans see what the
        # CI engineer did
        pr_number = state.get("pr_number")
        if pr_number:
            with contextlib.suppress(Exception):
                await self.scm.add_comment(
                    repo,
                    pr_number,
                    f"### CI Engineer Diagnosis\n\n"
                    f"**Decision:** `{decision.get('decision', 'unknown')}` "
                    f"(classification: `{decision.get('classification', 'unknown')}`)\n\n"
                    f"**Summary:** {decision.get('summary', '(none)')}\n\n"
                    + (
                        f"**Files modified:** {', '.join(decision.get('files_modified', []))}\n"
                        if decision.get("files_modified")
                        else ""
                    )
                    + f"\n_Posted by the DevAI CI Engineer agent — build run {build_run_id}._",
                )

        return decision

    @staticmethod
    def _parse_ci_fix_decision(text: str) -> dict[str, Any]:
        """Extract the JSON decision block from the agent's final message.

        Tolerant of code-fenced or bare JSON. Returns a sensible default
        if the agent didn't include a parseable block.
        """
        if not text:
            return {"decision": "escalate", "classification": "unknown", "summary": "no output"}

        # Find the LAST {...} block in the text — the agent may have
        # narrated reasoning before printing the final JSON.
        depth = 0
        end_idx = None
        start_idx = None
        for i in range(len(text) - 1, -1, -1):
            ch = text[i]
            if ch == "}":
                if depth == 0:
                    end_idx = i + 1
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start_idx = i
                    break

        if start_idx is None or end_idx is None:
            return {"decision": "escalate", "classification": "unknown", "summary": text[:300]}

        try:
            data = json.loads(text[start_idx:end_idx])
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            data.setdefault("decision", "escalate")
            data.setdefault("classification", "unknown")
            data.setdefault("summary", "")
            data.setdefault("files_modified", [])
            return data
        except (json.JSONDecodeError, ValueError):
            return {
                "decision": "escalate",
                "classification": "unknown",
                "summary": text[:300],
            }

    # ------------------------------------------------------------------
    # CI Workflow Builder
    # ------------------------------------------------------------------

    async def _ensure_ci_workflow_exists(
        self,
        repo: str,
        branch: str,
        state: ALMState,
        a2a: A2ABus,
    ) -> None:
        """If the repo has no .github/workflows on the branch, render the
        active skill profile's CI template and commit it.

        This is what makes the CI Agent a true "CI Builder" — it doesn't
        just watch builds, it bootstraps them when missing. The template
        is stack-aware (Next.js npm flow vs Go go-test vs Python pytest)
        because it comes from the SkillProfile selected by the Tech
        Detector.
        """
        profile = get_skill_profile(state.get("skill_profile_name"))
        if not profile.ci_workflow_template:
            logger.debug(
                "Skill profile %s has no CI template — skipping bootstrap",
                profile.name,
            )
            return

        try:
            # Check if any workflow file exists on the branch we're going
            # to build. We list .github/workflows; an empty/missing dir
            # means we should bootstrap.
            existing = await self.scm.list_files(repo, ".github/workflows", ref=branch)
            workflow_files = [
                item
                for item in (existing or [])
                if isinstance(item, dict) and item.get("name", "").endswith((".yml", ".yaml"))
            ]
            if workflow_files:
                logger.info(
                    "CI workflow already present on %s@%s (%d files), skipping bootstrap",
                    repo,
                    branch,
                    len(workflow_files),
                )
                return
        except Exception as e:
            # 404 from GitHub when the directory doesn't exist is the
            # expected path; treat it as "no workflow" and proceed.
            logger.debug("No existing workflows on %s@%s (%s) — will bootstrap", repo, branch, e)

        try:
            await self.scm.create_or_update_file(
                repo=repo,
                path=".github/workflows/ci.yml",
                content=profile.ci_workflow_template,
                message=f"ci: bootstrap workflow for {profile.display_name}",
                branch=branch,
            )
            logger.info(
                "Bootstrapped CI workflow for %s@%s using profile %s",
                repo,
                branch,
                profile.name,
            )
            a2a.notify(
                "senior_developer",
                "CI Workflow Bootstrapped",
                f"Created `.github/workflows/ci.yml` for {profile.display_name} "
                "since the repo had none. Future pushes will run lint + test + build.",
            )
        except Exception as e:
            # Don't fail the run if we can't bootstrap — the existing
            # build may still pass via repo-level config we couldn't see.
            logger.warning(
                "Failed to bootstrap CI workflow on %s@%s: %s — continuing",
                repo,
                branch,
                e,
            )
