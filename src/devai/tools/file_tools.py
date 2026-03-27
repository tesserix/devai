"""File operation tool definitions for Claude agents."""

from __future__ import annotations

from typing import Any

FILE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file from the repository via GitHub API.",
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
        "name": "write_file",
        "description": "Write content to a file by committing it to a branch via GitHub API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "path": {"type": "string", "description": "File path within the repo"},
                "content": {"type": "string", "description": "File content to write"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to commit to"},
            },
            "required": ["repo", "path", "content", "message", "branch"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories in a repository path.",
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
]
