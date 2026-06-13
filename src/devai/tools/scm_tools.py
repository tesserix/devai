"""Provider-agnostic SCM tool definitions for Claude agents (tool-use).

These tools work with any SCM provider (GitHub, GitLab, Azure DevOps)
through the unified SCMClient interface. Tool names use the `scm_` prefix
to indicate they are provider-agnostic.

For agents that use Claude's tool-use API, these tool schemas define the
available operations. The SCMToolExecutor handles actual execution by
delegating to the configured SCMClient.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)

# Provider-agnostic tool schemas for Claude's tool-use API
SCM_TOOLS: list[dict[str, Any]] = [
    {
        "name": "scm_get_issue",
        "description": "Get the details of an issue or work item including title, body, labels, and comments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository identifier (e.g., org/repo for GitHub, group/project for GitLab, org/project/repo for ADO)",
                },
                "issue_id": {"type": "integer", "description": "Issue number or work item ID"},
            },
            "required": ["repo", "issue_id"],
        },
    },
    {
        "name": "scm_create_issue",
        "description": "Create a new issue or work item with title, body, and optional labels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "title": {"type": "string", "description": "Issue/work item title"},
                "body": {"type": "string", "description": "Issue/work item body in Markdown"},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels or tags to apply",
                },
            },
            "required": ["repo", "title", "body"],
        },
    },
    {
        "name": "scm_close_issue",
        "description": "Close an issue or work item (e.g. after the release that ships it).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "issue_id": {"type": "integer", "description": "Issue number or work item ID"},
            },
            "required": ["repo", "issue_id"],
        },
    },
    {
        "name": "scm_add_comment",
        "description": "Add a comment to an issue, work item, or pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "issue_id": {"type": "integer", "description": "Issue/PR number or work item ID"},
                "body": {"type": "string", "description": "Comment body in Markdown"},
            },
            "required": ["repo", "issue_id", "body"],
        },
    },
    {
        "name": "scm_get_file_content",
        "description": "Get the contents of a file from a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "path": {"type": "string", "description": "File path within the repo"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "scm_list_files",
        "description": "List files in a directory of a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "path": {"type": "string", "description": "Directory path (empty for root)"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "scm_get_repo_tree",
        "description": "Get the complete file tree of a repository. Returns all file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "scm_create_branch",
        "description": "Create a new branch in a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "branch_name": {"type": "string", "description": "New branch name"},
                "from_branch": {"type": "string", "description": "Base branch (defaults to default branch)"},
            },
            "required": ["repo", "branch_name"],
        },
    },
    {
        "name": "scm_commit_file",
        "description": "Create or update a file in a repository by committing it to a branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "path": {"type": "string", "description": "File path within the repo"},
                "content": {"type": "string", "description": "File content"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to commit to"},
            },
            "required": ["repo", "path", "content", "message", "branch"],
        },
    },
    {
        "name": "scm_create_pull_request",
        "description": "Create a pull request or merge request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "title": {"type": "string", "description": "PR/MR title"},
                "body": {"type": "string", "description": "PR/MR description in Markdown"},
                "head": {"type": "string", "description": "Head/source branch name"},
                "base": {"type": "string", "description": "Base/target branch (defaults to default branch)"},
            },
            "required": ["repo", "title", "body", "head"],
        },
    },
    {
        "name": "scm_get_pr_diff",
        "description": "Get the diff of a pull request or merge request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "pr_id": {"type": "integer", "description": "Pull request or merge request number/ID"},
            },
            "required": ["repo", "pr_id"],
        },
    },
    {
        "name": "scm_create_pr_review",
        "description": "Submit a review on a pull request (APPROVE, REQUEST_CHANGES, or COMMENT).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "pr_id": {"type": "integer", "description": "Pull request number/ID"},
                "body": {"type": "string", "description": "Review body"},
                "event": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": "Review event type",
                },
            },
            "required": ["repo", "pr_id", "body", "event"],
        },
    },
    {
        "name": "scm_get_pull_request",
        "description": "Get a pull request's metadata: title, state, mergeability, head/base branches, author.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "pr_id": {"type": "integer", "description": "Pull request number/ID"},
            },
            "required": ["repo", "pr_id"],
        },
    },
    {
        "name": "scm_merge_pull_request",
        "description": "Merge a pull request or merge request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "pr_id": {"type": "integer", "description": "Pull request number/ID"},
                "method": {
                    "type": "string",
                    "enum": ["squash", "merge", "rebase"],
                    "description": "Merge method (default: squash)",
                },
            },
            "required": ["repo", "pr_id"],
        },
    },
    {
        "name": "scm_get_pipeline_runs",
        "description": "Get recent CI/CD pipeline or workflow runs for a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "branch": {"type": "string", "description": "Filter by branch name (optional)"},
                "limit": {"type": "integer", "description": "Maximum number of runs to return (default: 5)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "scm_ci_status",
        "description": (
            "Ground-truth CI verdict for a branch: the newest run of EVERY workflow with its "
            "conclusion (success/failure/in_progress). Use this to verify your fix actually turned "
            "the builds green before declaring success — narration doesn't count, this does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "branch": {"type": "string", "description": "Branch to check (the story/work branch)"},
            },
            "required": ["repo", "branch"],
        },
    },
    {
        "name": "scm_ci_failure_logs",
        "description": (
            "Failure forensics for a red workflow run: failed job names, failed steps, and a log "
            "tail (secrets redacted). Start every debug round here — read the ACTUAL errors, then "
            "fix the workflow file or the code, push to the branch, and re-check scm_ci_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "run_id": {
                    "type": "string",
                    "description": "Workflow run id or its html URL (from scm_ci_status)",
                },
            },
            "required": ["repo", "run_id"],
        },
    },
    {
        "name": "scm_rerun_failed_jobs",
        "description": (
            "Re-run only the FAILED jobs of a workflow run — re-test after a fix or to rule out a "
            "flake without pushing an empty commit. Poll scm_ci_status afterwards for the verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository identifier"},
                "run_id": {"type": "string", "description": "Workflow run id to re-run failed jobs for"},
            },
            "required": ["repo", "run_id"],
        },
    },
]


def _legacy_tool_aliases(prefix: str = "github_") -> list[dict[str, Any]]:
    """Expose SCM tools under legacy names for existing agents."""

    aliased_tools: list[dict[str, Any]] = []
    for tool in SCM_TOOLS:
        name = tool["name"]
        aliased_tools.append({**tool, "name": name.replace("scm_", prefix, 1)})
    return aliased_tools


# Backward-compatible aliases (old github_* names → new scm_* names)
GITHUB_TOOLS = _legacy_tool_aliases()


class SCMToolExecutor:
    """Executes SCM tools called by Claude agents via any provider.

    This is the provider-agnostic replacement for GitHubToolExecutor.
    Delegates all operations to the configured SCMClient.
    All operations pass through guardrails for authorization and audit.
    """

    def __init__(
        self,
        scm: SCMClient,
        agent_name: str = "",
        *,
        run_id: str = "",
        redis: Any = None,
        triggered_by: str = "",
        trace_id: str = "",
    ) -> None:
        self.scm = scm
        self._agent_name = agent_name
        # When set, file-write tools push entries to
        # ``devai:run:<run_id>:repo_events`` so the dashboard's REPO tab
        # sees changes in real time. All four are optional — passing
        # nothing degrades to the legacy no-event behavior.
        self._run_id = run_id
        self._redis = redis
        self._triggered_by = triggered_by
        self._trace_id = trace_id

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute an SCM tool and return the result as a string."""
        import json

        # Liveness heartbeat: every tool call proves the agent is actively
        # working. The executor's stage supervision reads this to extend the
        # stage deadline while progress continues (and to kill stalls fast).
        if self._run_id and self._redis is not None:
            import time as _time

            try:
                await self._redis.set(f"devai:run:{self._run_id}:activity", str(_time.time()), ex=3600)
            except Exception:  # noqa: BLE001 — heartbeat is best-effort
                pass

        # Support both scm_* and legacy github_* tool names
        normalized = tool_name.replace("github_", "scm_")
        handler = getattr(self, f"_handle_{normalized}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"

        # Guardrail: validate file paths and branch names
        self._validate_inputs(normalized, tool_input)

        # Guardrail: filter output through secret masking
        result = await handler(tool_input)
        if isinstance(result, str):
            return self._filter_output(result)
        return self._filter_output(json.dumps(result, indent=2, default=str))

    def _validate_inputs(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Validate tool inputs through guardrails."""
        try:
            from devai.services.guardrails import InputSanitizer

            sanitizer = InputSanitizer()

            # Validate file paths
            if "path" in tool_input and tool_name in ("scm_commit_file", "scm_get_file_content"):
                sanitizer.validate_file_path(tool_input["path"])

            # Validate branch names
            if "branch_name" in tool_input:
                sanitizer.validate_branch_name(tool_input["branch_name"])
            if "branch" in tool_input:
                sanitizer.validate_branch_name(tool_input["branch"])

            # Validate commit content
            if tool_name == "scm_commit_file" and "content" in tool_input:
                from devai.services.guardrails import OutputFilter

                warnings = OutputFilter().validate_commit_content(
                    tool_input.get("path", ""),
                    tool_input["content"],
                )
                blocking = [w for w in warnings if w.startswith("BLOCK:")]
                if blocking:
                    raise PermissionError(f"Commit blocked by guardrails: {'; '.join(blocking)}")

        except ImportError:
            pass  # Guardrails not available

    def _filter_output(self, text: str) -> str:
        """Filter tool output through secret masking."""
        try:
            from devai.services.guardrails import OutputFilter

            return OutputFilter().filter_for_scm(text)
        except ImportError:
            return text

    async def _handle_scm_get_issue(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.get_issue(inp["repo"], inp["issue_id"])

    async def _handle_scm_create_issue(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.create_issue(
            inp["repo"],
            inp["title"],
            inp["body"],
            inp.get("labels"),
        )

    async def _handle_scm_add_comment(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.add_comment(inp["repo"], inp["issue_id"], inp["body"])

    async def _handle_scm_get_file_content(self, inp: dict[str, Any]) -> str:
        # Exploration misses are NORMAL agent behavior — answer them with a
        # clear, actionable sentence instead of a raw HTTP 404 (which reads
        # as a scary error in the turn log and costs the model a turn of
        # confusion interpreting it).
        try:
            return await self.scm.get_file_content(inp["repo"], inp["path"], inp.get("ref"))
        except FileNotFoundError as e:
            # Directory-not-file (and similar) — already a clear sentence.
            return str(e)
        except Exception as e:  # noqa: BLE001
            if "404" in str(e):
                return (
                    f"File {inp['path']!r} does not exist on this ref — check the path "
                    "with scm_list_files or create it with scm_commit_file."
                )
            raise

    async def _handle_scm_list_files(self, inp: dict[str, Any]) -> list[dict[str, Any]] | str:
        try:
            return await self.scm.list_files(inp["repo"], inp.get("path", ""), inp.get("ref"))
        except Exception as e:  # noqa: BLE001
            if "404" in str(e):
                return (
                    f"Directory {inp.get('path', '')!r} does not exist on this ref — "
                    "list the repo root or create files under it with scm_commit_file."
                )
            raise

    async def _handle_scm_get_repo_tree(self, inp: dict[str, Any]) -> list[dict[str, Any]]:
        tree = await self.scm.get_repo_tree(inp["repo"], inp.get("ref"))
        return [{"path": item["path"], "type": item["type"]} for item in tree]

    async def _record_workbranch(self, branch: str) -> None:
        """Record the branch the agent is ACTUALLY working on — ground truth
        from real tool calls, not the briefed branch name (LLMs sometimes
        pick their own). The dashboard's REPO tab follows this key live."""
        if not branch or not self._run_id or self._redis is None:
            return
        try:
            await self._redis.set(f"devai:run:{self._run_id}:workbranch", branch, ex=86400 * 7)
        except Exception:  # noqa: BLE001 — visibility aid, never fail the tool
            logger.debug("workbranch record failed for run=%s", self._run_id, exc_info=True)

    async def _handle_scm_create_branch(self, inp: dict[str, Any]) -> str:
        sha = await self.scm.create_branch(inp["repo"], inp["branch_name"], inp.get("from_branch"))
        await self._record_workbranch(inp.get("branch_name", ""))
        return f"Branch '{inp['branch_name']}' created at {sha}"

    async def _handle_scm_commit_file(self, inp: dict[str, Any]) -> dict[str, Any]:
        result = await self.scm.create_or_update_file(
            inp["repo"],
            inp["path"],
            inp["content"],
            inp["message"],
            inp["branch"],
        )
        await self._record_workbranch(inp.get("branch", ""))
        # Fan out to the dashboard's REPO tab. The "kind" is best-effort:
        # GitHub returns a "type" field on create vs update, but most
        # providers don't — we treat every commit as a "modified" event
        # since the dashboard cares about *what changed*, not whether
        # the path existed before.
        if self._run_id and self._redis is not None:
            from devai.webhook.repo_routes import emit_repo_event

            await emit_repo_event(
                self._redis,
                self._run_id,
                kind="modified",
                path=inp.get("path", ""),
                agent=self._agent_name,
                triggered_by=self._triggered_by,
                trace_id=self._trace_id,
            )
        return result

    async def _handle_scm_create_pull_request(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.create_pull_request(
            inp["repo"],
            inp["title"],
            inp["body"],
            inp["head"],
            inp.get("base"),
        )

    async def _handle_scm_get_pr_diff(self, inp: dict[str, Any]) -> str:
        return await self.scm.get_pr_diff(inp["repo"], inp["pr_id"])

    async def _handle_scm_get_pull_request(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.get_pull_request(inp["repo"], inp["pr_id"])

    async def _handle_scm_close_issue(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.update_issue(inp["repo"], inp["issue_id"], state="closed")

    async def _handle_scm_create_pr_review(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.create_pr_review(
            inp["repo"],
            inp["pr_id"],
            inp["body"],
            inp["event"],
        )

    async def _handle_scm_merge_pull_request(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.scm.merge_pull_request(
            inp["repo"],
            inp["pr_id"],
            inp.get("method", "squash"),
        )

    async def _handle_scm_get_pipeline_runs(self, inp: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.scm.get_pipeline_runs(
            inp["repo"],
            inp.get("branch"),
            inp.get("limit", 5),
        )

    async def _handle_scm_ci_status(self, inp: dict[str, Any]) -> dict[str, Any]:
        from devai.services.ci_insight import latest_ci_conclusions

        return await latest_ci_conclusions(self.scm, inp["repo"], inp.get("branch", ""))

    async def _handle_scm_ci_failure_logs(self, inp: dict[str, Any]) -> str:
        from devai.services.ci_insight import failed_job_logs

        logs = await failed_job_logs(self.scm, inp["repo"], inp.get("run_id", ""))
        return logs or "No failed jobs found for that run (or logs unavailable)."

    async def _handle_scm_rerun_failed_jobs(self, inp: dict[str, Any]) -> dict[str, Any]:
        from devai.services.ci_insight import rerun_failed_jobs

        return await rerun_failed_jobs(self.scm, inp["repo"], inp.get("run_id", ""))


# Backward-compatible alias
GitHubToolExecutor = SCMToolExecutor
