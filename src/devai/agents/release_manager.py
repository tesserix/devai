"""Release Manager Agent — handles deployment orchestration via ArgoCD.

In the collaborative parallel model, the Release Manager has two roles:
1. run_merge_stories(): Merge all approved story PRs to main sequentially
2. run() (deploy): Monitor ArgoCD deployment after merge

For Tesserix repos, deployment goes through ArgoCD (GitOps).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)


class ReleaseManagerAgent(BaseAgent):
    """Orchestrates deployment after tests pass, using ArgoCD for GitOps."""

    name = "release_manager"
    subscribe_subject = "devai.pipeline.tests_complete"
    publish_subject = ""

    # ------------------------------------------------------------------
    # Story Merge Phase — merge all approved story PRs
    # ------------------------------------------------------------------

    async def run_merge_stories(self, state: ALMState, a2a: A2ABus | None = None) -> dict[str, Any]:
        """Merge all approved story PRs to main, sequentially to handle conflicts."""
        from devai.graph.a2a import A2ABus as A2ABusClass

        if a2a is None:
            a2a = A2ABusClass(self.name, state.get("a2a_messages", []))

        repo = state.get("repo_full_name", "")
        story_branches = state.get("story_branches", [])

        approved_stories = [sb for sb in story_branches if sb.get("status") == "approved"]
        failed_stories = [sb for sb in story_branches if sb.get("status") == "failed"]

        if not approved_stories:
            a2a.escalate(
                "supervisor",
                "No Approved Stories to Merge",
                f"Out of {len(story_branches)} stories, none were approved. {len(failed_stories)} failed.",
            )
            return {
                "deploy_status": "failed",
                "error": "No approved stories to merge",
                "a2a_messages": state.get("a2a_messages", []) + a2a.collect_outbox(),
            }

        # Notify everyone about merge phase
        a2a.broadcast(
            "Merging Story PRs",
            f"Merging {len(approved_stories)} approved story PRs to main.\n"
            + "\n".join(
                f"- PR #{sb.get('pr_number', '?')} — Story #{sb.get('story_number', '?')}: {sb.get('story_title', '')}"
                for sb in approved_stories
            ),
        )

        merged_prs: list[dict[str, Any]] = []
        merge_errors: list[str] = []

        for sb in approved_stories:
            pr_number = sb.get("pr_number")
            story_number = sb.get("story_number", "?")

            if not pr_number:
                merge_errors.append(f"Story #{story_number}: no PR number")
                continue

            merge_result = await self._merge_pr(repo, pr_number)

            if merge_result.get("merged", False):
                merged_prs.append(
                    {
                        "pr_number": pr_number,
                        "story_number": story_number,
                        "sha": merge_result.get("sha", ""),
                    }
                )
                a2a.notify(
                    "orchestrator",
                    f"Story #{story_number} Merged",
                    f"PR #{pr_number} (Story #{story_number}) merged to main. SHA: {merge_result.get('sha', '')[:8]}",
                )
            else:
                error = merge_result.get("message", "Merge failed")
                merge_errors.append(f"Story #{story_number} PR #{pr_number}: {error}")
                a2a.escalate(
                    "senior_developer",
                    f"Story #{story_number} Merge Failed",
                    f"Could not merge PR #{pr_number}: {error}\n"
                    "This may be a merge conflict. The developer should resolve it.",
                )

        # Update story_branches with merge status
        updated_branches = list(story_branches)
        for i, sb in enumerate(updated_branches):
            if sb.get("status") == "approved":
                pr_num = sb.get("pr_number")
                if any(m["pr_number"] == pr_num for m in merged_prs):
                    updated_branches[i] = {**sb, "status": "merged"}

        # Summary
        merged = len(merged_prs)
        errors = len(merge_errors)

        summary = (
            f"Merge complete: {merged}/{len(approved_stories)} PRs merged. "
            f"{errors} errors. {len(failed_stories)} stories had failed quality gates."
        )

        if merge_errors:
            summary += "\n\nMerge errors:\n" + "\n".join(f"- {e}" for e in merge_errors)

        a2a.broadcast("Merge Phase Complete", summary)

        last_sha = merged_prs[-1]["sha"][:8] if merged_prs else ""

        return {
            "story_branches": updated_branches,
            "deploy_version": last_sha,
            "deploy_status": "merging_complete" if merged > 0 else "failed",
            "error": "\n".join(merge_errors) if merge_errors else None,
            "a2a_messages": state.get("a2a_messages", []) + a2a.collect_outbox(),
        }

    # ------------------------------------------------------------------
    # Deploy Phase — ArgoCD deployment after merge
    # ------------------------------------------------------------------

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Monitor ArgoCD deployment after all story PRs are merged."""
        repo = state.get("repo_full_name", "")

        # Check merge status
        deploy_status = state.get("deploy_status", "")
        if deploy_status == "failed":
            return {
                "deploy_status": "failed",
                "error": state.get("error", "Merge phase failed"),
            }

        # Notify everyone deployment is starting
        story_branches = state.get("story_branches", [])
        merged_count = sum(1 for sb in story_branches if sb.get("status") == "merged")
        a2a.broadcast(
            "Deployment Starting",
            f"Deploying {merged_count} merged stories to production via ArgoCD.",
        )

        # Wait for ArgoCD sync and health
        argocd_app = self._derive_argocd_app_name(repo)
        deploy_result = await self._wait_for_argocd_deployment(argocd_app, a2a)
        argocd_status = deploy_result.get("result", "unknown")

        # Rollback if deployment failed
        if argocd_status in ("degraded", "failed"):
            a2a.escalate(
                "supervisor",
                "Deployment Failed — Rolling Back",
                f"ArgoCD app '{argocd_app}' is {argocd_status}. "
                f"Health: {deploy_result.get('health_status', 'unknown')}. "
                "Initiating rollback to previous version.",
            )
            rollback_result = await self._rollback(argocd_app)
            deploy_result["rollback"] = rollback_result

        # Generate release summary
        release_summary = await self._generate_release_summary(state)

        # Post deployment comment on the tracking issue
        await self._update_tracking_issue(state, argocd_status, state.get("deploy_version", ""), deploy_result)

        # Notify completion
        is_healthy = argocd_status == "healthy"
        a2a.broadcast(
            f"Deployment {'Successful' if is_healthy else 'Failed'}",
            f"Deployed to production via ArgoCD.\n"
            f"ArgoCD app: {argocd_app}\n"
            f"Status: {argocd_status}\n"
            f"Stories merged: {merged_count}\n\n{release_summary}",
        )

        return {
            "deploy_status": "success" if is_healthy else "failed",
            "deploy_version": state.get("deploy_version", ""),
            "deploy_environment": "production",
            "deploy_argocd_app": argocd_app,
            "health_check_passed": is_healthy,
        }

    # ------------------------------------------------------------------
    # Helper Methods
    # ------------------------------------------------------------------

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

            try:
                status = await argocd.get_app_status(app_name)
                logger.info(
                    "ArgoCD app %s: sync=%s health=%s",
                    app_name,
                    status["sync_status"],
                    status["health_status"],
                )
            except Exception:
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
        """Derive the ArgoCD Application name from the repo name."""
        return repo.split("/")[-1] if "/" in repo else repo

    async def _generate_release_summary(self, state: ALMState) -> str:
        """Generate a release summary covering all merged stories."""
        try:
            from devai.agents.skills import get_skill_profile

            openai = OpenAIProvider(self.config)
            profile = get_skill_profile(state.get("skill_profile_name"))

            story_branches = state.get("story_branches", [])
            merged = [sb for sb in story_branches if sb.get("status") == "merged"]
            story_list = "\n".join(
                f"- Story #{sb.get('story_number', '?')}: {sb.get('story_title', '')}" for sb in merged
            )

            response = await openai.generate(
                prompt=f"""Generate a brief release summary for these changes:

Stack: {profile.display_name}
Requirements: {state.get("requirements", "")[:500]}

Merged Stories:
{story_list}

Total stories: {len(merged)} merged, {len(story_branches)} total

{profile.render_for_release() or "Release flow: use the project's standard flow."}

Write a concise 2-3 sentence release note suitable for a changelog.""",
                system="You are a technical writer. Write concise, professional release notes.",
                max_tokens=256,
            )
            return response

        except Exception:
            return "Release deployed successfully."

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

        story_branches = state.get("story_branches", [])
        merged = [sb for sb in story_branches if sb.get("status") == "merged"]
        failed = [sb for sb in story_branches if sb.get("status") == "failed"]

        icon = "white_check_mark" if status == "healthy" else "x"
        stories_table = "\n".join(
            f"| #{sb.get('story_number', '?')} | {sb.get('story_title', '')[:40]} | "
            f"PR #{sb.get('pr_number', '?')} | {sb.get('status', '?')} |"
            for sb in story_branches
        )

        body = (
            f"### Deployment Complete\n\n"
            f":{icon}: **{status.upper()}**\n\n"
            f"**Stories Merged:** {len(merged)}\n"
            f"**Stories Failed:** {len(failed)}\n"
            f"**ArgoCD App:** `{argocd_result.get('name', 'unknown')}`\n"
            f"**Sync Status:** {argocd_result.get('sync_status', 'N/A')}\n"
            f"**Health Status:** {argocd_result.get('health_status', 'N/A')}\n\n"
            f"| Story | Title | PR | Status |\n|---|---|---|---|\n{stories_table}\n"
        )

        try:
            await self.scm.add_comment(repo, tracking, body)
        except Exception as e:
            logger.warning("Failed to update tracking issue: %s", e)
