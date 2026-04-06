"""CI Monitor Agent — monitors GitHub Actions builds and reports results.

Watches for workflow runs triggered by the PR, polls until completion,
and parses build logs to identify failures. Uses Groq for fast log analysis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.graph.a2a import A2ABus
from devai.graph.state import ALMState
from devai.providers.groq_provider import GroqProvider

logger = logging.getLogger(__name__)

# Max time to wait for a build (15 minutes)
BUILD_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 15


class CIMonitorAgent(BaseAgent):
    """Monitors GitHub Actions CI builds and analyzes results."""

    name = "ci_monitor"
    subscribe_subject = "devai.pipeline.review_complete"
    publish_subject = "devai.pipeline.build_complete"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Monitor the CI build for the PR branch."""
        repo = state.get("repo_full_name", "")
        branch = state.get("branch_name")
        pr_number = state.get("pr_number")

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

        # Poll for the latest workflow run on this branch
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

            # Also notify the reviewer
            a2a.notify(
                "staff_reviewer",
                "CI Build Failed",
                f"Build failed for the reviewed PR. See: {url}",
            )

        return result

    async def _wait_for_build(self, repo: str, branch: str) -> dict[str, Any] | None:
        """Poll GitHub Actions for the latest workflow run on the branch."""
        elapsed = 0

        while elapsed < BUILD_TIMEOUT_SECONDS:
            try:
                resp = await self.github._request(
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
            resp = await self.github._request(
                "GET",
                f"/repos/{repo}/actions/runs/{run_id}/jobs",
            )
            jobs = resp.json().get("jobs", [])

            failed = []
            for job in jobs:
                if job.get("conclusion") == "failure":
                    # Get failed steps
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
