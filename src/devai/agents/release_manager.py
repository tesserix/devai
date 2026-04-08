"""Release Manager Agent — handles deployment orchestration via ArgoCD.

For Tesserix repos, deployment goes through ArgoCD (GitOps).
This agent:
1. Merges the approved PR (squash merge)
2. Monitors ArgoCD Application sync status via kubectl
3. Waits for pod health checks to pass
4. Supports rollback if deployment fails
5. Reports deployment status on the PR and tracking issue
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)


class ReleaseManagerAgent(BaseAgent):
    """Orchestrates deployment after tests pass, using ArgoCD for GitOps."""

    name = "release_manager"
    subscribe_subject = "devai.pipeline.tests_complete"
    publish_subject = ""

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Merge PR and monitor ArgoCD deployment."""
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

        # Step 2: Wait for ArgoCD sync and health
        argocd_app = self._derive_argocd_app_name(repo)
        deploy_result = await self._wait_for_argocd_deployment(argocd_app, a2a)
        deploy_status = deploy_result.get("result", "unknown")

        # Step 3: Rollback if deployment failed
        if deploy_status in ("degraded", "failed"):
            a2a.escalate(
                "supervisor",
                "Deployment Failed — Rolling Back",
                f"ArgoCD app '{argocd_app}' is {deploy_status}. "
                f"Health: {deploy_result.get('health_status', 'unknown')}. "
                "Initiating rollback to previous version.",
            )
            rollback_result = await self._rollback(argocd_app)
            deploy_result["rollback"] = rollback_result

        # Step 4: Generate release summary
        release_summary = await self._generate_release_summary(state)

        # Step 5: Post deployment comment on PR and tracking issue
        is_healthy = deploy_status == "healthy"
        await self._post_deployment_comment(
            repo,
            pr_number,
            deploy_status,
            merge_sha,
            release_summary,
            deploy_result,
        )
        await self._update_tracking_issue(state, deploy_status, merge_sha, deploy_result)

        # Notify completion
        a2a.broadcast(
            f"Deployment {'Successful' if is_healthy else 'Failed'}",
            f"PR #{pr_number} deployed to production.\n"
            f"ArgoCD app: {argocd_app}\n"
            f"Commit: {merge_sha[:8]}\n"
            f"Status: {deploy_status}\n"
            f"Sync took: {deploy_result.get('elapsed', '?')}s\n\n{release_summary}",
        )

        return {
            "deploy_status": "success" if is_healthy else "failed",
            "deploy_version": merge_sha[:8],
            "deploy_environment": "production",
            "deploy_argocd_app": argocd_app,
            "health_check_passed": is_healthy,
        }

    async def _merge_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Merge the PR using squash merge via SCM client."""
        try:
            return await self.scm.merge_pull_request(repo, pr_number, method="squash")
        except Exception as e:
            logger.error("Failed to merge PR #%d: %s", pr_number, e)
            return {"merged": False, "message": str(e)}

    async def _wait_for_argocd_deployment(
        self,
        app_name: str,
        a2a: A2ABus,
    ) -> dict[str, Any]:
        """Wait for ArgoCD Application to sync and become healthy."""
        try:
            from devai.services.argocd import ArgocdClient

            argocd = ArgocdClient(self.config)

            # First, check if the app exists
            try:
                status = await argocd.get_app_status(app_name)
                logger.info(
                    "ArgoCD app %s: sync=%s health=%s",
                    app_name,
                    status["sync_status"],
                    status["health_status"],
                )
            except Exception:
                # App might not exist yet (first deployment)
                logger.warning(
                    "ArgoCD app '%s' not found — it may need to be created via tesserix-k8s",
                    app_name,
                )
                a2a.notify(
                    "infra_provisioner",
                    "ArgoCD App Not Found",
                    f"Application '{app_name}' not found in ArgoCD. "
                    "Ensure the ArgoCD Application manifest is committed to tesserix-k8s.",
                )
                return {"result": "not_found", "name": app_name}

            # Wait for sync completion
            a2a.notify(
                "orchestrator",
                "Waiting for ArgoCD Sync",
                f"Monitoring ArgoCD app '{app_name}' for sync completion...",
            )

            result = await argocd.wait_for_sync(app_name)

            if result["result"] == "healthy":
                logger.info("ArgoCD app %s deployed successfully", app_name)
            elif result["result"] == "timeout":
                logger.warning("ArgoCD sync timed out for %s", app_name)
            else:
                logger.error(
                    "ArgoCD deployment issue for %s: %s",
                    app_name,
                    result.get("result"),
                )

            return result

        except ImportError:
            logger.warning("ArgoCD client not available — skipping deployment verification")
            return {"result": "skipped", "name": app_name}
        except Exception as e:
            logger.error("ArgoCD deployment check failed: %s", e)
            return {"result": "error", "name": app_name, "error": str(e)}

    async def _rollback(self, app_name: str) -> dict[str, Any]:
        """Rollback to the previous ArgoCD revision."""
        try:
            from devai.services.argocd import ArgocdClient

            argocd = ArgocdClient(self.config)
            result = await argocd.rollback(app_name)
            logger.info("Rollback initiated for %s: %s", app_name, result)
            return result
        except Exception as e:
            logger.error("Rollback failed for %s: %s", app_name, e)
            return {"error": str(e)}

    def _derive_argocd_app_name(self, repo: str) -> str:
        """Derive the ArgoCD Application name from the repo name.

        Convention: tesserix/my-service → my-service
        """
        return repo.split("/")[-1] if "/" in repo else repo

    async def _generate_release_summary(self, state: ALMState) -> str:
        """Generate a release summary using Groq."""
        try:
            groq = GroqProvider(self.config)

            stories = state.get("stories", [])
            story_list = "\n".join(f"- {s.get('title', 'untitled')}" for s in stories[:10])

            response = await groq.generate(
                prompt=f"""Generate a brief release summary for these changes:

Requirements: {state.get("requirements", "")[:500]}

User Stories:
{story_list}

Test Results: {state.get("test_passed", 0)} passed, {state.get("test_failed", 0)} failed

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
        argocd_result: dict[str, Any],
    ) -> None:
        """Post deployment status as a PR comment."""
        icon = "white_check_mark" if status == "healthy" else "warning" if status == "timeout" else "x"
        body = (
            f"## Deployment Status\n\n"
            f":{icon}: **{status.upper()}**\n\n"
            f"**Commit:** `{sha[:8]}`\n"
            f"**Environment:** production\n"
            f"**ArgoCD App:** `{argocd_result.get('name', 'unknown')}`\n"
            f"**Sync:** {argocd_result.get('sync_status', 'N/A')}\n"
            f"**Health:** {argocd_result.get('health_status', 'N/A')}\n"
            f"**Duration:** {argocd_result.get('elapsed', '?')}s\n\n"
            f"### Release Notes\n{summary}"
        )

        if argocd_result.get("rollback"):
            rollback = argocd_result["rollback"]
            body += (
                f"\n\n### Rollback\n"
                f":arrows_counterclockwise: Rolled back to revision `{rollback.get('target_revision', '?')}`"
            )

        try:
            await self.scm.add_comment(repo, pr_number, body)
        except Exception as e:
            logger.error("Failed to post deployment comment: %s", e)

    async def _update_tracking_issue(
        self,
        state: ALMState,
        status: str,
        sha: str,
        argocd_result: dict[str, Any],
    ) -> None:
        """Post deployment result on the Supervisor's tracking issue."""
        repo = state.get("repo_full_name", "")
        tracking = state.get("supervisor_tracking_issue")
        if not repo or not tracking:
            return

        icon = "white_check_mark" if status == "healthy" else "x"
        body = (
            f"### Deployment Complete\n\n"
            f":{icon}: **{status.upper()}**\n\n"
            f"| Field | Value |\n|---|---|\n"
            f"| Commit | `{sha[:8]}` |\n"
            f"| ArgoCD App | `{argocd_result.get('name', 'unknown')}` |\n"
            f"| Sync Status | {argocd_result.get('sync_status', 'N/A')} |\n"
            f"| Health Status | {argocd_result.get('health_status', 'N/A')} |\n"
            f"| Duration | {argocd_result.get('elapsed', '?')}s |\n"
        )

        try:
            await self.scm.add_comment(repo, tracking, body)
        except Exception as e:
            logger.warning("Failed to update tracking issue: %s", e)
