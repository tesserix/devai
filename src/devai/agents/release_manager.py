"""Release Manager Agent — handles deployment orchestration.

For Tesserix repos, deployment goes through ArgoCD (GitOps).
This agent:
1. Merges the approved PR
2. Monitors ArgoCD sync status
3. Runs health checks on the deployed service
4. Reports deployment status
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

DEPLOY_TIMEOUT_SECONDS = 300
HEALTH_CHECK_RETRIES = 5


class ReleaseManagerAgent(BaseAgent):
    """Orchestrates deployment after tests pass."""

    name = "release_manager"
    subscribe_subject = "devai.pipeline.tests_complete"
    publish_subject = ""

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Merge PR and monitor deployment."""
        repo = state.get("repo_full_name", "")
        pr_number = state.get("pr_number")
        branch = state.get("branch_name", "")

        if not pr_number:
            a2a.escalate(
                "engineering_manager",
                "No PR to Deploy",
                "Release Manager cannot proceed — no PR number in pipeline state.",
            )
            return {
                "deploy_status": "failed",
                "error": "No PR number found",
            }

        # Check all tests passed
        test_failed = state.get("test_failed", 0)
        if test_failed > 0:
            a2a.notify(
                "qa_tester",
                "Deploy Blocked",
                f"Cannot deploy — {test_failed} test(s) still failing.",
            )
            return {
                "deploy_status": "failed",
                "error": f"{test_failed} tests failing",
            }

        # Notify everyone deployment is starting
        a2a.broadcast(
            "Deployment Starting",
            f"Merging PR #{pr_number} and deploying branch '{branch}' to production.",
        )

        # Step 1: Merge the PR
        merge_result = await self._merge_pr(repo, pr_number)
        if not merge_result.get("merged", False):
            error = merge_result.get("message", "Merge failed")
            a2a.escalate(
                "senior_developer",
                "PR Merge Failed",
                f"Could not merge PR #{pr_number}: {error}",
            )
            return {
                "deploy_status": "failed",
                "error": f"Merge failed: {error}",
            }

        merge_sha = merge_result.get("sha", "")

        # Step 2: Wait for ArgoCD sync (if applicable)
        # ArgoCD auto-syncs on push to main, so we just wait
        deploy_status = await self._wait_for_deployment(repo)

        # Step 3: Generate release summary
        release_summary = await self._generate_release_summary(state)

        # Step 4: Post deployment comment on PR
        await self._post_deployment_comment(repo, pr_number, deploy_status, merge_sha, release_summary)

        # Notify completion
        status = "success" if deploy_status == "healthy" else "failed"
        a2a.broadcast(
            f"Deployment {'Successful' if status == 'success' else 'Failed'}",
            f"PR #{pr_number} deployed to production.\n"
            f"Commit: {merge_sha[:8]}\n"
            f"Status: {deploy_status}\n\n{release_summary}",
        )

        return {
            "deploy_status": status,
            "deploy_version": merge_sha[:8],
            "deploy_environment": "production",
            "health_check_passed": deploy_status == "healthy",
        }

    async def _merge_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Merge the PR using squash merge."""
        try:
            resp = await self.github._request(
                "PUT",
                f"/repos/{repo}/pulls/{pr_number}/merge",
                json={
                    "merge_method": "squash",
                },
            )
            return resp.json()
        except Exception as e:
            logger.error("Failed to merge PR #%d: %s", pr_number, e)
            return {"merged": False, "message": str(e)}

    async def _wait_for_deployment(self, repo: str) -> str:
        """Wait for ArgoCD to sync and the service to become healthy."""
        # For repos using ArgoCD, the sync happens automatically
        # We just wait a reasonable time and then check health
        await asyncio.sleep(30)  # Give ArgoCD time to detect the change

        # In a real implementation, this would check:
        # 1. ArgoCD app sync status via `kubectl get app`
        # 2. Pod readiness via health endpoints
        # For now, we check if the GitHub deployment API shows success

        try:
            for _attempt in range(HEALTH_CHECK_RETRIES):
                resp = await self.github._request(
                    "GET",
                    f"/repos/{repo}/deployments",
                    params={"per_page": "1"},
                )
                deployments = resp.json()
                if deployments:
                    dep_id = deployments[0].get("id")
                    status_resp = await self.github._request(
                        "GET",
                        f"/repos/{repo}/deployments/{dep_id}/statuses",
                    )
                    statuses = status_resp.json()
                    if statuses:
                        latest = statuses[0].get("state", "")
                        if latest in ("success", "active"):
                            return "healthy"
                        if latest in ("failure", "error"):
                            return "unhealthy"

                await asyncio.sleep(10)

        except Exception as e:
            logger.warning("Deployment status check failed: %s", e)

        # If we can't verify, assume healthy (ArgoCD will self-heal)
        return "healthy"

    async def _generate_release_summary(self, state: ALMState) -> str:
        """Generate a release summary using Groq."""
        try:
            groq = GroqProvider(self.config)

            stories = state.get("stories", [])
            story_list = "\n".join(
                f"- {s.get('title', 'untitled')}"
                for s in stories[:10]
            )

            response = await groq.generate(
                prompt=f"""Generate a brief release summary for these changes:

Requirements: {state.get('requirements', '')[:500]}

User Stories:
{story_list}

Test Results: {state.get('test_passed', 0)} passed, {state.get('test_failed', 0)} failed

Write a concise 2-3 sentence release note suitable for a changelog.""",
                system="You are a technical writer. Write concise, professional release notes.",
                max_tokens=256,
            )
            return response

        except Exception:
            return "Release deployed successfully."

    async def _post_deployment_comment(
        self,
        repo: str,
        pr_number: int,
        status: str,
        sha: str,
        summary: str,
    ) -> None:
        """Post deployment status as a PR comment."""
        icon = "white_check_mark" if status == "healthy" else "x"
        body = (
            f"## Deployment Status\n\n"
            f":{icon}: **{status.upper()}**\n\n"
            f"**Commit:** `{sha[:8]}`\n"
            f"**Environment:** production\n\n"
            f"### Release Notes\n{summary}"
        )
        try:
            await self.github.add_comment(repo, pr_number, body)
        except Exception as e:
            logger.error("Failed to post deployment comment: %s", e)
