"""GitHub tool definitions — backward-compatible re-exports from scm_tools.

All tool functionality has moved to scm_tools.py which provides
provider-agnostic tools that work with GitHub, GitLab, and Azure DevOps.
This module re-exports everything for backward compatibility.
"""

from devai.tools.scm_tools import (  # noqa: F401
    GITHUB_TOOLS,
    SCM_TOOLS,
    GitHubToolExecutor,
    SCMToolExecutor,
)

__all__ = [
    "GITHUB_TOOLS",
    "SCM_TOOLS",
    "GitHubToolExecutor",
    "SCMToolExecutor",
]
