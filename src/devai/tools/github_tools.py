"""GitHub tool definitions for Claude agents (tool-use)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.core.github_client import GitHubClient

# Tool schemas for Claude's tool-use API
GITHUB_TOOLS: list[dict[str, Any]] = [
    {
        "name": "github_get_issue",
        "description": "Get the details of a GitHub issue including title, body, labels, and comments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "issue_number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "github_create_issue",
        "description": "Create a new GitHub issue with title, body, and optional labels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body in Markdown"},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to apply",
                },
            },
            "required": ["repo", "title", "body"],
        },
    },
    {
        "name": "github_add_comment",
        "description": "Add a comment to a GitHub issue or pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "issue_number": {"type": "integer", "description": "Issue or PR number"},
                "body": {"type": "string", "description": "Comment body in Markdown"},
            },
            "required": ["repo", "issue_number", "body"],
        },
    },
    {
        "name": "github_get_file_content",
        "description": "Get the contents of a file from a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "path": {"type": "string", "description": "File path within the repo"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "github_list_files",
        "description": "List files in a directory of a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "path": {"type": "string", "description": "Directory path (empty for root)"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "github_get_repo_tree",
        "description": "Get the complete file tree of a repository. Returns all file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "ref": {"type": "string", "description": "Branch or commit SHA (optional)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "github_create_branch",
        "description": "Create a new branch in a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch_name": {"type": "string", "description": "New branch name"},
                "from_branch": {"type": "string", "description": "Base branch (defaults to default branch)"},
            },
            "required": ["repo", "branch_name"],
        },
    },
    {
        "name": "github_commit_file",
        "description": "Create or update a file in a GitHub repository by committing it to a branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "path": {"type": "string", "description": "File path within the repo"},
                "content": {"type": "string", "description": "File content"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to commit to"},
            },
            "required": ["repo", "path", "content", "message", "branch"],
        },
    },
    {
        "name": "github_create_pull_request",
        "description": "Create a pull request in a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description in Markdown"},
                "head": {"type": "string", "description": "Head branch name"},
                "base": {"type": "string", "description": "Base branch (defaults to default branch)"},
            },
            "required": ["repo", "title", "body", "head"],
        },
    },
    {
        "name": "github_get_pr_diff",
        "description": "Get the diff of a pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "pr_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "github_create_pr_review",
        "description": "Submit a review on a pull request (APPROVE, REQUEST_CHANGES, or COMMENT).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "pr_number": {"type": "integer", "description": "Pull request number"},
                "body": {"type": "string", "description": "Review body"},
                "event": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": "Review event type",
                },
            },
            "required": ["repo", "pr_number", "body", "event"],
        },
    },
]


class GitHubToolExecutor:
    """Executes GitHub tools called by Claude agents."""

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a GitHub tool and return the result as a string."""
        import json

        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"

        result = await handler(tool_input)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)

    async def _handle_github_get_issue(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.get_issue(inp["repo"], inp["issue_number"])

    async def _handle_github_create_issue(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.create_issue(
            inp["repo"], inp["title"], inp["body"], inp.get("labels"),
        )

    async def _handle_github_add_comment(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.add_comment(inp["repo"], inp["issue_number"], inp["body"])

    async def _handle_github_get_file_content(self, inp: dict[str, Any]) -> str:
        return await self.github.get_file_content(inp["repo"], inp["path"], inp.get("ref"))

    async def _handle_github_list_files(self, inp: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.github.list_files(inp["repo"], inp.get("path", ""), inp.get("ref"))

    async def _handle_github_get_repo_tree(self, inp: dict[str, Any]) -> list[dict[str, Any]]:
        tree = await self.github.get_repo_tree(inp["repo"], inp.get("ref"))
        # Return only paths and types to keep response concise
        return [{"path": item["path"], "type": item["type"]} for item in tree]

    async def _handle_github_create_branch(self, inp: dict[str, Any]) -> str:
        sha = await self.github.create_branch(inp["repo"], inp["branch_name"], inp.get("from_branch"))
        return f"Branch '{inp['branch_name']}' created at {sha}"

    async def _handle_github_commit_file(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.create_or_update_file(
            inp["repo"], inp["path"], inp["content"], inp["message"], inp["branch"],
        )

    async def _handle_github_create_pull_request(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.create_pull_request(
            inp["repo"], inp["title"], inp["body"], inp["head"], inp.get("base"),
        )

    async def _handle_github_get_pr_diff(self, inp: dict[str, Any]) -> str:
        return await self.github.get_pr_diff(inp["repo"], inp["pr_number"])

    async def _handle_github_create_pr_review(self, inp: dict[str, Any]) -> dict[str, Any]:
        return await self.github.create_pr_review(
            inp["repo"], inp["pr_number"], inp["body"], inp["event"],
        )
