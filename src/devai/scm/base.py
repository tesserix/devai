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

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Marker embedded in issue bodies so future agent runs can recognize their
# own prior work and avoid creating duplicates. Kept as an HTML comment so
# it doesn't render in the GitHub UI.
DEDUPE_MARKER = "devai-fingerprint"

_TITLE_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "to",
    "in",
    "on",
    "and",
    "or",
    "with",
    "by",
    "is",
    "be",
    "as",
    "at",
    "from",
    "this",
    "that",
    "into",
    "via",
    "across",
    "over",
    "under",
    "about",
    "after",
    "before",
    "between",
    "request",
    "task",
    "issue",
    "operational",
    "operation",
    "operations",
    "devai",
}


def _normalize_title_tokens(text: str) -> set[str]:
    """Normalize a title into a token set for Jaccard similarity.

    Lowercases, strips punctuation, drops common stopwords + DevAI-specific
    boilerplate so titles like '[DevAI] Operational task — re-run...' and
    '[DevAI] Operations request: Re-run...' collapse to the same token set.
    """
    if not text:
        return set()
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return {t for t in cleaned.split() if len(t) > 2 and t not in _TITLE_STOPWORDS}


def fingerprint_for(title: str, body: str = "", extra: str = "") -> str:
    """Compute a stable 12-char fingerprint for a proposed issue.

    Hashes a normalized version of (title + first 200 body chars + extra).
    Two re-runs of the same agent on the same intent produce identical
    fingerprints, which is the strongest dedup signal.
    """
    seed = " ".join((title or "", (body or "")[:200], extra or "")).strip().lower()
    seed = re.sub(r"\s+", " ", seed)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


class SCMProvider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    AZURE_DEVOPS = "azure_devops"


class AuthMethod(StrEnum):
    GITHUB_APP = "github_app"  # JWT + Installation Token
    PAT = "pat"  # Personal Access Token (GitHub/GitLab)
    OAUTH = "oauth"  # OAuth2 token
    ADO_PAT = "ado_pat"  # Azure DevOps PAT
    GITLAB_TOKEN = "gitlab_token"  # GitLab project/group token


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
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue (GitHub/GitLab) or work item (ADO)."""
        ...

    @abstractmethod
    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        """Get an issue/work item by number or ID."""
        ...

    async def list_issues(
        self,
        repo: str,
        state: str = "open",
        labels: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List issues. Default returns []. Providers should override.

        Used by ``find_duplicate_issue`` / ``create_issue_idempotent`` for
        cross-run deduplication so the same agent re-running the same intent
        doesn't spam the repo with copies of the same issue.
        """
        return []

    async def find_duplicate_issue(
        self,
        repo: str,
        title: str,
        labels: list[str] | None = None,
        fingerprint: str | None = None,
        similarity_threshold: float = 0.6,
    ) -> dict[str, Any] | None:
        """Look for an existing issue that's a likely duplicate of this one.

        Searches BOTH open and recently-closed issues. The closed-issue
        search catches the common case where a previous run finished a
        story and closed its issue, then a re-run tries to recreate the
        same story-issue from scratch.

        Match wins on either:
          1. Body contains the exact ``<!-- devai-fingerprint: X -->`` marker
             (strongest signal — survives LLM title rewrites)
          2. Title token Jaccard similarity >= ``similarity_threshold`` AND
             at least one label overlaps

        The label overlap requirement prevents collisions between unrelated
        DevAI work items that happen to share generic words.
        """
        # Pull both open and recently-closed candidates so we can dedup
        # against issues that an earlier successful run already finished.
        open_candidates = await self.list_issues(repo, state="open", labels=labels, limit=100)
        closed_candidates = await self.list_issues(repo, state="closed", labels=labels, limit=50)
        candidates = list(open_candidates) + list(closed_candidates)
        if not candidates:
            return None

        # Strongest signal: explicit fingerprint marker in the body
        if fingerprint:
            marker = f"<!-- {DEDUPE_MARKER}: {fingerprint} -->"
            for issue in candidates:
                body = issue.get("body") or ""
                if marker in body:
                    return issue

        # Fallback: title similarity (open issues only — we don't want to
        # silently re-use a closed issue based on fuzzy title alone, that
        # could cause cross-feature collisions. The fingerprint check
        # above already handles the legitimate "same intent" case.)
        target_tokens = _normalize_title_tokens(title)
        if not target_tokens:
            return None

        target_labels = set(labels or [])
        best_issue: dict[str, Any] | None = None
        best_score = 0.0

        for issue in open_candidates:
            cand_tokens = _normalize_title_tokens(issue.get("title", ""))
            if not cand_tokens:
                continue
            jaccard = len(target_tokens & cand_tokens) / len(target_tokens | cand_tokens)
            if jaccard < similarity_threshold:
                continue

            # Require at least one label overlap if we asked for any labels
            if target_labels:
                cand_labels = {lbl.get("name") if isinstance(lbl, dict) else lbl for lbl in (issue.get("labels") or [])}
                if not (target_labels & cand_labels):
                    continue

            if jaccard > best_score:
                best_score = jaccard
                best_issue = issue

        return best_issue

    async def create_issue_idempotent(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        dedupe_labels: list[str] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Create an issue, but skip if a duplicate already exists.

        Args:
            repo: target repository
            title, body, labels: same as ``create_issue``
            dedupe_labels: labels used to scope the duplicate search.
                Defaults to ``labels``. Pass a smaller subset (e.g. just
                ``["devai:user-story"]``) when you want loose matching.
            fingerprint: optional pre-computed fingerprint. If omitted, one
                is auto-derived from title + first 200 chars of body.

        Behavior:
            - If a duplicate is found → posts an attribution comment on the
              existing issue and returns it.
            - Otherwise → creates a new issue with a hidden fingerprint
              marker embedded in the body so future runs match.
        """
        if not fingerprint:
            fingerprint = fingerprint_for(title, body)

        search_labels = dedupe_labels if dedupe_labels is not None else labels

        try:
            existing = await self.find_duplicate_issue(
                repo=repo,
                title=title,
                labels=search_labels,
                fingerprint=fingerprint,
            )
        except Exception as e:
            logger.warning("Dedup search failed on %s, will create normally: %s", repo, e)
            existing = None

        if existing:
            issue_number = existing.get("number")
            logger.info(
                "Dedup hit on %s: re-using #%s instead of creating duplicate %r",
                repo,
                issue_number,
                title[:60],
            )
            try:
                await self.add_comment(
                    repo,
                    issue_number,
                    "_DevAI agent attempted to create another issue with the same intent. "
                    "Re-using this existing issue instead._\n\n"
                    f"_Proposed title:_ `{title}`",
                )
            except Exception as e:
                logger.debug("Could not add dedup comment to #%s: %s", issue_number, e)
            return existing

        marker = f"\n\n<!-- {DEDUPE_MARKER}: {fingerprint} -->"
        body_with_marker = (body or "").rstrip() + marker
        return await self.create_issue(repo, title, body_with_marker, labels)

    @abstractmethod
    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        """Add a comment to an issue/work item."""
        ...

    async def list_issue_comments(
        self,
        repo: str,
        issue_id: int | str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List issue comments in chronological order when supported."""
        return []

    @abstractmethod
    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        """Add labels/tags to an issue."""
        ...

    async def update_issue(
        self,
        repo: str,
        issue_id: int | str,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Edit an issue's title/body/state. Default: unsupported.

        Concrete default (not abstract) so providers that haven't wired it
        yet keep working — callers treat this as best-effort (e.g. appending
        the story task-list to the epic body for GitHub tracked-issues).
        """
        raise NotImplementedError(f"{type(self).__name__} does not support update_issue")

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
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file by committing it to a branch."""
        ...

    # --- Pull Requests / Merge Requests ---

    @abstractmethod
    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        """Create a pull request (GitHub), merge request (GitLab), or PR (ADO).

        When ``draft`` is true the PR is opened in draft / work-in-progress
        state and must be marked ready (``mark_pull_request_ready``) before
        it can be merged.
        """
        ...

    async def mark_pull_request_ready(self, repo: str, pr_id: int) -> dict[str, Any]:
        """Promote a draft PR to ready-for-review (a real PR).

        Default raises so non-GitHub backends degrade clearly; GitHub
        overrides it. Returns provider-specific PR data on success.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support marking PRs ready for review")

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
        self,
        repo: str,
        pr_id: int,
        body: str,
        event: str = "COMMENT",
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

    # --- Repo Visibility (for CI build limits on private repos) ---

    async def set_repo_visibility(self, repo: str, visibility: str) -> dict[str, Any]:
        """Set repository visibility ('public' or 'private').

        Used for the public→build→private cycle when the org has limited
        Actions minutes for private repos.

        Default implementation is a no-op — override in provider.
        """
        return {"visibility": visibility}

    async def get_repo_visibility(self, repo: str) -> str:
        """Get current repository visibility.

        Returns 'public' or 'private'.
        """
        info = await self.get_repo_info(repo)
        return info.get("visibility", "private")

    async def get_all_workflow_runs(
        self,
        repo: str,
        status: str = "in_progress",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get all workflow runs with a given status.

        Used to check if ALL builds have completed before toggling visibility.
        Default returns empty — override in provider.
        """
        return []

    # --- Projects / Boards ---

    async def create_project(
        self,
        org: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a project board (GitHub Projects v2, GitLab Board, ADO Board).

        Returns: dict with at least 'id' and 'url' keys.
        Default implementation returns empty — override in provider.
        """
        return {"id": None, "url": ""}

    async def add_item_to_project(
        self,
        project_id: str,
        content_id: str,
    ) -> dict[str, Any]:
        """Add an issue or PR to a project board.

        Args:
            project_id: The project node ID.
            content_id: The issue/PR node ID.

        Returns: dict with at least 'item_id' key.
        """
        return {"item_id": None}

    async def get_node_id(self, repo: str, issue_number: int) -> str:
        """Get the GraphQL node ID for an issue.

        Required for adding issues to GitHub Projects v2.
        Default returns empty string — override in provider.
        """
        return ""

    # --- Repo onboarding (Repos page) ---

    async def set_branch_protection(self, repo: str, branch: str, *, required_approvals: int = 1) -> dict[str, Any]:
        """Enable a baseline branch-protection gate on ``branch``.

        Default no-op — override in providers that support it."""
        return {}

    async def list_installation_repos(self, per_page: int = 100, org: str = "") -> list[dict[str, Any]]:
        """List every repo this credential can see (GitHub App installation
        or token scope).

        ``org`` scopes the listing to a single organization when the provider
        supports it (avoids walking every org a broad token can see). Default
        empty — override in provider."""
        return []

    async def probe_markers(self, repos: list[tuple[str, str, str]], marker_path: str) -> dict[str, bool]:
        """Return {``owner/name``: marker_present} for the given repos.

        ``repos`` is a list of ``(owner, name, ref)`` tuples. The default
        implementation probes each repo's file contents one-by-one — a
        provider with a batch API (e.g. GitHub GraphQL) should override
        to fold it into a single round trip.
        """
        out: dict[str, bool] = {}
        for owner, name, ref in repos:
            full = f"{owner}/{name}"
            try:
                await self.get_file_content(repo=full, path=marker_path, ref=ref)
                out[full] = True
            except Exception:  # noqa: BLE001
                out[full] = False
        return out

    async def request_reviewers(self, repo: str, pr_id: int, reviewers: list[str]) -> dict[str, Any]:
        """Request reviewers on a pull request (assign-to-approve flow).

        Default no-op — override in provider.
        """
        return {"requested": []}

    # --- Lifecycle ---

    @abstractmethod
    async def close(self) -> None:
        """Close the HTTP client."""
        ...
