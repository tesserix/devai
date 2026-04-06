"""Abstract SCM client — defines the contract all providers must implement.

Every agent in the pipeline calls these methods. The concrete implementation
(GitHub, GitLab, ADO) handles the API differences behind this interface.

Auth methods:
  - GITHUB_APP: JWT → Installation Token (automated, org-wide)
  - PAT:        Personal Access Token (simple, user-scoped)
  - OAUTH:      OAuth2 token from dashboard login
  - ADO_PAT:    Azure DevOps PAT (Base64 encoded in header)
  - GITLAB_TOKEN: GitLab project/group access token

Connection flow:
  1. Dashboard user selects repo from any provider
  2. Config stores: scm_provider, scm_base_url, auth_method, credentials
  3. create_scm_client(config) returns the right concrete client
  4. All agents call client.create_issue(), client.get_file_content(), etc.
  5. The abstraction normalizes everything — agents never know which provider
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class SCMProvider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    AZURE_DEVOPS = "azure_devops"


class AuthMethod(str, Enum):
    GITHUB_APP = "github_app"      # JWT + Installation Token
    PAT = "pat"                     # Personal Access Token (GitHub/GitLab)
    OAUTH = "oauth"                 # OAuth2 token
    ADO_PAT = "ado_pat"            # Azure DevOps PAT
    GITLAB_TOKEN = "gitlab_token"   # GitLab project/group token


class SCMClient(ABC):
    """Abstract source code management client.

    All methods use a normalized `repo` identifier:
      - GitHub:    "org/repo"
      - GitLab:    "group/project" or project ID
      - ADO:       "org/project/repo"
    """

    provider: SCMProvider

    # --- Issues / Work Items ---

    @abstractmethod
    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue (GitHub/GitLab) or work item (ADO)."""
        ...

    @abstractmethod
    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        """Get an issue/work item by number or ID."""
        ...

    @abstractmethod
    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        """Add a comment to an issue/work item."""
        ...

    @abstractmethod
    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        """Add labels/tags to an issue."""
        ...

    # --- Branches ---

    @abstractmethod
    async def get_default_branch(self, repo: str) -> str:
        """Get the default branch name."""
        ...

    @abstractmethod
    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        """Create a new branch. Returns the SHA/commit of the new branch."""
        ...

    # --- Files ---

    @abstractmethod
    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        """Get file content from the repository."""
        ...

    @abstractmethod
    async def list_files(self, repo: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        """List files in a directory."""
        ...

    @abstractmethod
    async def get_repo_tree(self, repo: str, ref: str | None = None) -> list[dict[str, Any]]:
        """Get the full file tree of a repository."""
        ...

    @abstractmethod
    async def create_or_update_file(
        self, repo: str, path: str, content: str, message: str, branch: str, sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file by committing it to a branch."""
        ...

    # --- Pull Requests / Merge Requests ---

    @abstractmethod
    async def create_pull_request(
        self, repo: str, title: str, body: str, head: str, base: str | None = None,
    ) -> dict[str, Any]:
        """Create a pull request (GitHub), merge request (GitLab), or PR (ADO)."""
        ...

    @abstractmethod
    async def get_pull_request(self, repo: str, pr_id: int) -> dict[str, Any]:
        """Get a pull request."""
        ...

    @abstractmethod
    async def get_pr_diff(self, repo: str, pr_id: int) -> str:
        """Get the diff of a pull request."""
        ...

    @abstractmethod
    async def create_pr_review(
        self, repo: str, pr_id: int, body: str, event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a review on a pull request."""
        ...

    @abstractmethod
    async def merge_pull_request(self, repo: str, pr_id: int, method: str = "squash") -> dict[str, Any]:
        """Merge a pull request."""
        ...

    # --- CI/CD ---

    @abstractmethod
    async def get_pipeline_runs(self, repo: str, branch: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Get CI pipeline/workflow runs."""
        ...

    @abstractmethod
    async def get_pipeline_jobs(self, repo: str, run_id: int | str) -> list[dict[str, Any]]:
        """Get jobs within a pipeline run."""
        ...

    # --- Repository ---

    @abstractmethod
    async def get_repo_info(self, repo: str) -> dict[str, Any]:
        """Get repository metadata (name, default branch, visibility, etc.)."""
        ...

    # --- Webhooks ---

    @abstractmethod
    def verify_webhook_signature(self, body: bytes, signature: str, secret: str) -> bool:
        """Verify an incoming webhook signature."""
        ...

    @abstractmethod
    def parse_webhook_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a webhook event into a normalized trigger format.

        Returns None if the event should be ignored.
        Returns dict with: repo, issue_number, title, body, action, trigger_type
        """
        ...

    # --- Lifecycle ---

    @abstractmethod
    async def close(self) -> None:
        """Close the HTTP client."""
        ...
