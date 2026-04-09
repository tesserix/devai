"""GitHub SCM client — supports GitHub App (JWT) and PAT auth.

Auth modes:
  1. GitHub App: Generates JWT → exchanges for installation token
     - Best for: Org-wide automation, webhook-driven pipelines
     - Config: github_app_id + github_app_private_key + github_app_installation_id

  2. PAT (Personal Access Token): Direct token in Authorization header
     - Best for: Individual repos, quick setup, testing
     - Config: scm_token (fine-grained or classic PAT)

  3. OAuth: User token from dashboard login
     - Best for: Dashboard-triggered runs using user's permissions
     - Config: token passed per-request from session
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
import jwt

from devai.scm.base import AuthMethod, SCMClient, SCMProvider

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubSCMClient(SCMClient):
    """GitHub implementation of the SCM interface."""

    provider = SCMProvider.GITHUB

    def __init__(
        self,
        base_url: str = GITHUB_API,
        auth_method: AuthMethod = AuthMethod.PAT,
        token: str = "",
        app_id: int = 0,
        app_private_key: str = "",
        installation_id: int = 0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_method = auth_method
        self._token = token
        self._app_id = app_id
        self._app_private_key = app_private_key
        self._installation_id = installation_id
        self._installation_token: str | None = None
        self._token_expires_at: float = 0
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
            headers={"Accept": "application/vnd.github+json"},
        )

    # --- Auth ---

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": str(self._app_id)}
        return jwt.encode(payload, self._app_private_key, algorithm="RS256")

    async def _get_token(self) -> str:
        """Get the appropriate auth token based on auth method."""
        if self._auth_method == AuthMethod.PAT or self._auth_method == AuthMethod.OAUTH:
            return self._token

        if self._auth_method == AuthMethod.GITHUB_APP:
            if self._installation_token and time.time() < self._token_expires_at - 60:
                return self._installation_token
            app_jwt = self._generate_jwt()
            resp = await self._http.post(
                f"/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._installation_token = data["token"]
            self._token_expires_at = time.time() + 3600
            return self._installation_token  # type: ignore[return-value]

        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._get_token()
        headers = {"Authorization": f"token {token}"} if token else {}
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.is_client_error or resp.is_server_error:
            body = resp.text[:500]
            logger.error("GitHub API %s %s → %s: %s", method, path, resp.status_code, body)
            resp.raise_for_status()
        return resp

    # --- Repositories ---

    async def list_installation_repos(self, per_page: int = 100) -> list[dict[str, Any]]:
        """List repos accessible to the GitHub App installation."""
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._request("GET", f"/installation/repositories?per_page={per_page}&page={page}")
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append(
                    {
                        "full_name": r["full_name"],
                        "name": r["name"],
                        "description": r.get("description") or "",
                        "language": r.get("language") or "",
                        "private": r.get("private", False),
                        "default_branch": r.get("default_branch", "main"),
                    }
                )
            if len(data.get("repositories", [])) < per_page:
                break
            page += 1
        return repos

    async def create_repo(self, org: str, name: str, description: str = "", private: bool = True) -> dict[str, Any]:
        """Create a new repository in an organization."""
        resp = await self._request(
            "POST",
            f"/orgs/{org}/repos",
            json={"name": name, "description": description, "private": private, "auto_init": True},
        )
        data = resp.json()
        return {
            "full_name": data["full_name"],
            "name": data["name"],
            "description": data.get("description") or "",
            "html_url": data["html_url"],
            "default_branch": data.get("default_branch", "main"),
        }

    # --- Issues ---

    async def ensure_labels(self, repo: str, labels: list[str]) -> None:
        """Create labels on a repo if they don't already exist."""
        import contextlib

        for label in labels:
            with contextlib.suppress(Exception):
                await self._request(
                    "POST",
                    f"/repos/{repo}/labels",
                    json={"name": label, "color": "6366f1"},
                )

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        # GitHub requires a non-empty title
        safe_title = (title or "").strip() or "Untitled Issue"
        safe_body = (body or "").strip() or ""
        if labels:
            await self.ensure_labels(repo, labels)
        payload: dict[str, Any] = {"title": safe_title, "body": safe_body}
        if labels:
            payload["labels"] = labels
        try:
            resp = await self._request("POST", f"/repos/{repo}/issues", json=payload)
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300] if exc.response else "no body"
            raise RuntimeError(
                f"Failed to create issue on {repo} (HTTP {exc.response.status_code}): {detail}. "
                f"Check that the repo exists and has Issues enabled."
            ) from exc

    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo}/issues/{issue_id}")
        return resp.json()

    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        resp = await self._request("POST", f"/repos/{repo}/issues/{issue_id}/comments", json={"body": body})
        return resp.json()

    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        await self._request("POST", f"/repos/{repo}/issues/{issue_id}/labels", json={"labels": labels})

    # --- Branches ---

    async def get_default_branch(self, repo: str) -> str:
        resp = await self._request("GET", f"/repos/{repo}")
        return resp.json()["default_branch"]

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        if from_branch is None:
            from_branch = await self.get_default_branch(repo)
        sha_resp = await self._request("GET", f"/repos/{repo}/git/ref/heads/{from_branch}")
        sha = sha_resp.json()["object"]["sha"]
        await self._request("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch_name}", "sha": sha})
        return sha

    # --- Files ---

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params=params)
        return base64.b64decode(resp.json()["content"]).decode("utf-8")

    async def list_files(self, repo: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params=params)
        return resp.json()

    async def get_repo_tree(self, repo: str, ref: str | None = None) -> list[dict[str, Any]]:
        if ref is None:
            ref = await self.get_default_branch(repo)
        sha_resp = await self._request("GET", f"/repos/{repo}/git/ref/heads/{ref}")
        sha = sha_resp.json()["object"]["sha"]
        resp = await self._request("GET", f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
        return resp.json().get("tree", [])

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: dict[str, Any] = {"message": message, "content": encoded, "branch": branch}
        if sha:
            payload["sha"] = sha
        resp = await self._request("PUT", f"/repos/{repo}/contents/{path}", json=payload)
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
        if base is None:
            base = await self.get_default_branch(repo)
        resp = await self._request(
            "POST", f"/repos/{repo}/pulls", json={"title": title, "body": body, "head": head, "base": base}
        )
        return resp.json()

    async def get_pull_request(self, repo: str, pr_id: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{pr_id}")
        return resp.json()

    async def get_pr_diff(self, repo: str, pr_id: int) -> str:
        token = await self._get_token()
        headers = {"Accept": "application/vnd.github.diff"}
        if token:
            headers["Authorization"] = f"token {token}"
        resp = await self._http.get(f"/repos/{repo}/pulls/{pr_id}", headers=headers)
        resp.raise_for_status()
        return resp.text

    async def create_pr_review(
        self,
        repo: str,
        pr_id: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        resp = await self._request("POST", f"/repos/{repo}/pulls/{pr_id}/reviews", json=payload)
        return resp.json()

    async def merge_pull_request(self, repo: str, pr_id: int, method: str = "squash") -> dict[str, Any]:
        resp = await self._request("PUT", f"/repos/{repo}/pulls/{pr_id}/merge", json={"merge_method": method})
        return resp.json()

    # --- CI/CD ---

    async def get_pipeline_runs(self, repo: str, branch: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, str] = {"per_page": str(limit)}
        if branch:
            params["branch"] = branch
        resp = await self._request("GET", f"/repos/{repo}/actions/runs", params=params)
        return resp.json().get("workflow_runs", [])

    async def get_pipeline_jobs(self, repo: str, run_id: int | str) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        return resp.json().get("jobs", [])

    # --- Repository ---

    async def get_repo_info(self, repo: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo}")
        return resp.json()

    # --- Webhooks ---

    def verify_webhook_signature(self, body: bytes, signature: str, secret: str) -> bool:
        if not signature or not secret:
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize GitHub webhook events."""
        if event_type == "issues" and payload.get("action") in ("labeled", "opened"):
            issue = payload["issue"]
            return {
                "repo": payload["repository"]["full_name"],
                "issue_number": issue["number"],
                "title": issue.get("title", ""),
                "body": issue.get("body", "") or "",
                "labels": [lbl["name"] for lbl in issue.get("labels", [])],
                "action": payload["action"],
                "trigger_type": "github_issue",
            }

        if event_type == "issue_comment" and payload.get("action") == "created":
            comment = payload.get("comment", {}).get("body", "")
            if comment.startswith("/devai"):
                issue = payload["issue"]
                return {
                    "repo": payload["repository"]["full_name"],
                    "issue_number": issue["number"],
                    "title": issue.get("title", ""),
                    "body": issue.get("body", "") or "",
                    "labels": [lbl["name"] for lbl in issue.get("labels", [])],
                    "action": "command",
                    "command": comment,
                    "trigger_type": "github_issue",
                }

        return None

    # --- Repo Visibility ---

    async def set_repo_visibility(self, repo: str, visibility: str) -> dict[str, Any]:
        """Toggle repo between public and private (for CI build limits)."""
        resp = await self._request(
            "PATCH",
            f"/repos/{repo}",
            json={"visibility": visibility},
        )
        result = resp.json()
        logger.info("Set %s visibility to %s", repo, visibility)
        return {"visibility": result.get("visibility", visibility)}

    async def get_all_workflow_runs(
        self,
        repo: str,
        status: str = "in_progress",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get all workflow runs with a given status (queued or in_progress)."""
        runs = []
        for check_status in ("queued", "in_progress"):
            resp = await self._request(
                "GET",
                f"/repos/{repo}/actions/runs",
                params={"status": check_status, "per_page": str(limit)},
            )
            runs.extend(resp.json().get("workflow_runs", []))
        return runs

    # --- Projects v2 (GraphQL) ---

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GitHub GraphQL query."""
        token = await self._get_token()
        resp = await self._http.post(
            "https://api.github.com/graphql",
            headers={"Authorization": f"bearer {token}"},
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data.get("data", {})

    async def list_org_projects(self, org: str) -> list[dict[str, Any]]:
        """List open GitHub Projects v2 for an organization."""
        result = await self._graphql(
            """
            query($org: String!) {
                organization(login: $org) {
                    projectsV2(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
                        nodes {
                            id
                            title
                            number
                            shortDescription
                            url
                            closed
                        }
                    }
                }
            }
            """,
            {"org": org},
        )
        projects = result.get("organization", {}).get("projectsV2", {}).get("nodes", [])
        return [
            {
                "id": p["id"],
                "title": p["title"],
                "number": p["number"],
                "description": p.get("shortDescription", ""),
                "url": p.get("url", ""),
            }
            for p in projects
            if not p.get("closed")
        ]

    async def link_repo_to_project(self, project_id: str, repo: str) -> None:
        """Link a repository to a GitHub Project v2."""
        owner, name = repo.split("/", 1)
        repo_data = await self._graphql(
            """
            query($owner: String!, $name: String!) {
                repository(owner: $owner, name: $name) { id }
            }
            """,
            {"owner": owner, "name": name},
        )
        repo_id = repo_data.get("repository", {}).get("id")
        if not repo_id:
            raise RuntimeError(f"Repository '{repo}' not found")

        await self._graphql(
            """
            mutation($projectId: ID!, $repositoryId: ID!) {
                linkProjectV2ToRepository(
                    input: {projectId: $projectId, repositoryId: $repositoryId}
                ) { clientMutationId }
            }
            """,
            {"projectId": project_id, "repositoryId": repo_id},
        )

    async def create_project(
        self,
        org: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a GitHub Projects v2 board on the org."""
        # First get the org node ID
        org_data = await self._graphql(
            "query($login: String!) { organization(login: $login) { id } }",
            {"login": org},
        )
        owner_id = org_data.get("organization", {}).get("id")
        if not owner_id:
            raise RuntimeError(f"Organization '{org}' not found or not accessible")

        # Create the project
        result = await self._graphql(
            """
            mutation($ownerId: ID!, $title: String!) {
                createProjectV2(input: {ownerId: $ownerId, title: $title}) {
                    projectV2 {
                        id
                        number
                        url
                        title
                    }
                }
            }
            """,
            {"ownerId": owner_id, "title": title},
        )

        project = result.get("createProjectV2", {}).get("projectV2", {})

        # Update description if provided (separate mutation)
        if description and project.get("id"):
            await self._graphql(
                """
                mutation($projectId: ID!, $readme: String!) {
                    updateProjectV2(input: {projectId: $projectId, readme: $readme}) {
                        projectV2 { id }
                    }
                }
                """,
                {"projectId": project["id"], "readme": description},
            )

        return {
            "id": project.get("id", ""),
            "number": project.get("number"),
            "url": project.get("url", ""),
            "title": project.get("title", title),
        }

    async def add_item_to_project(
        self,
        project_id: str,
        content_id: str,
    ) -> dict[str, Any]:
        """Add an issue or PR to a GitHub Projects v2 board."""
        result = await self._graphql(
            """
            mutation($projectId: ID!, $contentId: ID!) {
                addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                    item {
                        id
                    }
                }
            }
            """,
            {"projectId": project_id, "contentId": content_id},
        )
        item = result.get("addProjectV2ItemById", {}).get("item", {})
        return {"item_id": item.get("id", "")}

    async def get_node_id(self, repo: str, issue_number: int) -> str:
        """Get the GraphQL node ID for an issue."""
        owner, name = repo.split("/", 1)
        result = await self._graphql(
            """
            query($owner: String!, $name: String!, $number: Int!) {
                repository(owner: $owner, name: $name) {
                    issue(number: $number) { id }
                }
            }
            """,
            {"owner": owner, "name": name, "number": issue_number},
        )
        return result.get("repository", {}).get("issue", {}).get("id", "")

    async def close(self) -> None:
        await self._http.aclose()
