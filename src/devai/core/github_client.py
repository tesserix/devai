"""GitHub API client with GitHub App authentication."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx
import jwt

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_LABEL_MAX_LENGTH = 50


class GitHubClient:
    """GitHub API client using GitHub App authentication."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._installation_token: str | None = None
        self._token_expires_at: float = 0
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API,
            timeout=30.0,
            headers={"Accept": "application/vnd.github+json"},
        )

    # --- Authentication ---

    def _generate_jwt(self) -> str:
        """Generate a JWT for GitHub App authentication."""
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),
            "iss": str(self.config.github_app_id),
        }
        return jwt.encode(payload, self.config.github_app_private_key, algorithm="RS256")

    async def _get_installation_token(self) -> str:
        """Get or refresh the installation access token."""
        if self._installation_token and time.time() < self._token_expires_at - 60:
            return self._installation_token

        app_jwt = self._generate_jwt()
        resp = await self._http.post(
            f"/app/installations/{self.config.github_app_installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._installation_token = data["token"]
        # GitHub tokens expire in 1 hour
        self._token_expires_at = time.time() + 3600
        return self._installation_token  # type: ignore[return-value]

    async def _headers(self) -> dict[str, str]:
        """Get authenticated headers."""
        if self.config.is_github_app_configured:
            token = await self._get_installation_token()
            return {"Authorization": f"token {token}"}
        # Fallback to no auth (for local dev / testing)
        return {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated GitHub API request."""
        headers = await self._headers()
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.is_error:
            logger.error("GitHub API %s %s -> %s: %s", method, path, resp.status_code, self._error_detail(resp))
        resp.raise_for_status()
        return resp

    def _error_detail(self, response: httpx.Response) -> str:
        """Return a concise GitHub error summary from a failed response."""
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text[:500]

        message = payload.get("message", "GitHub API error")
        errors = payload.get("errors")
        if not errors:
            return str(message)

        parts: list[str] = []
        for err in errors:
            if isinstance(err, dict):
                code = err.get("code")
                field = err.get("field")
                err_message = err.get("message")
                pieces = [str(piece) for piece in (field, code, err_message) if piece]
                if pieces:
                    parts.append(": ".join(pieces))
            else:
                parts.append(str(err))

        if not parts:
            return str(message)
        return f"{message} ({'; '.join(parts)})"

    def _normalize_labels(self, labels: list[str] | None) -> list[str]:
        """Normalize labels to GitHub-safe names and remove duplicates."""
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in labels or []:
            if not isinstance(raw, str):
                continue
            label = " ".join(raw.strip().split())
            if not label:
                continue
            label = label[:GITHUB_LABEL_MAX_LENGTH]
            if label in seen:
                continue
            seen.add(label)
            normalized.append(label)
        return normalized

    async def ensure_labels(self, repo: str, labels: list[str] | None) -> list[str]:
        """Ensure labels exist and return the subset safe to include on issue creation."""
        usable_labels: list[str] = []
        for label in self._normalize_labels(labels):
            try:
                await self._request(
                    "POST",
                    f"/repos/{repo}/labels",
                    json={"name": label, "color": "6366f1"},
                )
            except httpx.HTTPStatusError as exc:
                response = exc.response
                detail = self._error_detail(response) if response is not None else str(exc)
                if response is not None and response.status_code == 422 and "already_exists" in detail:
                    usable_labels.append(label)
                    continue
                logger.warning("Skipping invalid GitHub label %r on %s: %s", label, repo, detail)
                continue

            usable_labels.append(label)
        return usable_labels

    # --- Issues ---

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a GitHub issue."""
        safe_title = (title or "").strip() or "Untitled Issue"
        safe_body = (body or "").strip()
        safe_labels = await self.ensure_labels(repo, labels)

        payload: dict[str, Any] = {"title": safe_title, "body": safe_body}
        if safe_labels:
            payload["labels"] = safe_labels

        try:
            resp = await self._request("POST", f"/repos/{repo}/issues", json=payload)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            detail = self._error_detail(response) if response is not None else str(exc)
            label_error = response is not None and response.status_code == 422 and "label" in detail.lower()
            if label_error and "labels" in payload:
                logger.warning("Retrying GitHub issue creation on %s without labels after 422: %s", repo, detail)
                retry_payload = {"title": safe_title, "body": safe_body}
                resp = await self._request("POST", f"/repos/{repo}/issues", json=retry_payload)
            else:
                raise RuntimeError(f"Failed to create issue on {repo}: {detail}") from exc

        issue = resp.json()
        logger.info("Created issue #%d on %s", issue["number"], repo)
        return issue

    async def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        """Get a GitHub issue."""
        resp = await self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        return resp.json()

    async def add_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        """Add a comment to an issue or PR."""
        resp = await self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return resp.json()

    async def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue."""
        safe_labels = await self.ensure_labels(repo, labels)
        if not safe_labels:
            return
        await self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/labels",
            json={"labels": safe_labels},
        )

    # --- Branches ---

    async def get_default_branch(self, repo: str) -> str:
        """Get the default branch name for a repo."""
        resp = await self._request("GET", f"/repos/{repo}")
        return resp.json()["default_branch"]

    async def get_ref_sha(self, repo: str, ref: str) -> str:
        """Get the SHA for a branch ref."""
        resp = await self._request("GET", f"/repos/{repo}/git/ref/heads/{ref}")
        return resp.json()["object"]["sha"]

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        """Create a new branch. Returns the SHA of the new branch."""
        if from_branch is None:
            from_branch = await self.get_default_branch(repo)
        sha = await self.get_ref_sha(repo, from_branch)
        await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        )
        logger.info("Created branch %s on %s from %s", branch_name, repo, from_branch)
        return sha

    # --- File Operations ---

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        """Get the contents of a file from a repo."""
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params=params)
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content

    async def list_files(self, repo: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        """List files in a directory."""
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params=params)
        return resp.json()

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file in a repo."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        resp = await self._request("PUT", f"/repos/{repo}/contents/{path}", json=payload)
        logger.info("Committed %s to %s/%s", path, repo, branch)
        return resp.json()

    # --- Pull Requests ---

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
    ) -> dict[str, Any]:
        """Create a pull request."""
        if base is None:
            base = await self.get_default_branch(repo)
        resp = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        pr = resp.json()
        logger.info("Created PR #%d on %s", pr["number"], repo)
        return pr

    async def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Get a pull request."""
        resp = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")
        return resp.json()

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Get the diff of a pull request."""
        resp = await self._http.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
            headers={
                **(await self._headers()),
                "Accept": "application/vnd.github.diff",
            },
        )
        resp.raise_for_status()
        return resp.text

    async def create_pr_review(
        self,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a PR review (APPROVE, REQUEST_CHANGES, or COMMENT)."""
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        resp = await self._request(
            "POST",
            f"/repos/{repo}/pulls/{pr_number}/reviews",
            json=payload,
        )
        return resp.json()

    # --- Repository ---

    async def get_repo_tree(self, repo: str, ref: str | None = None, recursive: bool = True) -> list[dict[str, Any]]:
        """Get the full file tree of a repo."""
        if ref is None:
            ref = await self.get_default_branch(repo)
        sha = await self.get_ref_sha(repo, ref)
        params = {"recursive": "1"} if recursive else {}
        resp = await self._request("GET", f"/repos/{repo}/git/trees/{sha}", params=params)
        return resp.json().get("tree", [])

    # --- Cleanup ---

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()
