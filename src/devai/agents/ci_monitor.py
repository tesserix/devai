"""CI Monitor Agent — monitors GitHub Actions builds and reports results.

Handles the tesserix private repo build limit:
1. Makes repo public before CI runs (limited Actions minutes on private repos)
2. Polls ALL workflow runs until every queued/in-progress run completes
3. Makes repo private again only after ALL builds finish
4. Never leaves repos public if builds are still running

Uses Groq for fast log analysis of build failures.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# Max time to wait for a build (15 minutes)
BUILD_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 15
# Max time to wait for ALL builds to drain before making private
DRAIN_TIMEOUT_SECONDS = 1200  # 20 minutes


class CIMonitorAgent(BaseAgent):
    """Monitors GitHub Actions CI builds with private repo visibility management."""

    name = "ci_monitor"
    subscribe_subject = "devai.pipeline.review_complete"
    publish_subject = "devai.pipeline.build_complete"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Monitor the CI build for the PR branch.

        Handles the public→build→private cycle for tesserix repos
        with limited Actions minutes on private repos.
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

        # Step 1: Make repo public for CI (private repos have limited minutes)
        original_visibility = await self._make_public_for_ci(repo, a2a)

        try:
            # Step 2: Poll for the latest workflow run on this branch
            run_data = await self._wait_for_build(repo, branch)

            if not run_data:
                a2a.notify(
                    "senior_developer",
                    "No CI Workflow Found",
                    f"No GitHub Actions workflow found for branch '{branch}'. "
                    "Proceeding without CI validation.",
                )
                return {
                    "build_status": "success",
                    "build_url": "",
                    "build_logs": "No workflow found — skipped CI check",
                }

            build_run_id = run_data.get("id", 0)
            status = run_data.get("conclusion", "unknown")
            url = run_data.get("html_url", "")

            result: dict[str, Any] = {
                "build_run_id": build_run_id,
                "build_status": status,
                "build_url": url,
            }

            if status == "success":
                a2a.notify(
                    "qa_tester",
                    "CI Build Passed",
                    f"Build #{build_run_id} passed for branch '{branch}'.\nURL: {url}",
                )
                a2a.broadcast(
                    "Build Passed",
                    f"CI build #{build_run_id} passed on branch '{branch}'.",
                    exclude=["qa_tester"],
                )
            else:
                # Fetch and analyze failed job logs
                failed_jobs = await self._get_failed_jobs(repo, build_run_id)
                result["failed_jobs"] = failed_jobs

                # Use Groq to analyze the failure
                log_summary = await self._analyze_failure(failed_jobs)
                result["build_logs"] = log_summary

                # Notify the developer about the failure
                a2a.escalate(
                    "senior_developer",
                    "CI Build Failed",
                    f"Build #{build_run_id} failed on branch '{branch}'.\n\n"
                    f"## Failure Analysis\n{log_summary}\n\nURL: {url}",
                    payload={"failed_jobs": failed_jobs},
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
                    f"Temporarily set {repo} to public for CI build. "
                    "Will revert to private after ALL builds complete.",
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
                    len(active_runs), repo, run_ids,
                )
            except Exception as e:
                logger.warning("Failed to check active builds: %s", e)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        # Timeout — make private anyway but warn
        logger.warning(
            "Timed out waiting for builds on %s after %ds — making private anyway",
            repo, DRAIN_TIMEOUT_SECONDS,
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
                        step["name"]
                        for step in job.get("steps", [])
                        if step.get("conclusion") == "failure"
                    ]
                    failed.append({
                        "name": job.get("name", "unknown"),
                        "failed_steps": ", ".join(failed_steps),
                        "url": job.get("html_url", ""),
                    })
            return failed

        except Exception as e:
            logger.error("Failed to get job details: %s", e)
            return [{"name": "unknown", "failed_steps": str(e), "url": ""}]

    async def _analyze_failure(self, failed_jobs: list[dict[str, str]]) -> str:
        """Use Groq to analyze build failures and suggest fixes."""
        if not failed_jobs:
            return "No failed jobs found"

        try:
            groq = GroqProvider(self.config)

            job_details = "\n".join(
                f"- Job: {j['name']}, Failed Steps: {j['failed_steps']}"
                for j in failed_jobs
            )

            response = await groq.generate(
                prompt=f"Analyze these CI build failures and suggest fixes:\n\n{job_details}",
                system="You are a CI/CD expert. Analyze build failures concisely. "
                       "Identify root causes and suggest specific fixes. Be brief.",
                max_tokens=1024,
            )
            return response

        except Exception as e:
            logger.error("Failed to analyze build failure: %s", e)
            return f"Failed jobs: {', '.join(j['name'] for j in failed_jobs)}"
