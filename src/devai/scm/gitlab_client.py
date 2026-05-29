"""GitLab SCM client — supports PAT and project/group access tokens.

Auth modes:
  1. PAT: Personal or project access token in PRIVATE-TOKEN header
     - Config: scm_token
  2. OAuth: OAuth2 token in Authorization header
     - Config: token from OAuth flow

GitLab API: https://docs.gitlab.com/ee/api/
Repo format: "group/project" or numeric project ID
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import quote_plus

import httpx

from devai.scm.base import AuthMethod, SCMClient, SCMProvider

logger = logging.getLogger(__name__)


class GitLabSCMClient(SCMClient):
    """GitLab implementation of the SCM interface."""

    provider = SCMProvider.GITLAB

    def __init__(
        self,
        base_url: str = "https://gitlab.com",
        auth_method: AuthMethod = AuthMethod.PAT,
        token: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_method = auth_method
        self._token = token
        self._http = httpx.AsyncClient(base_url=f"{self._base_url}/api/v4", timeout=30.0)

    def _project_id(self, repo: str) -> str:
        """URL-encode the project path for GitLab API."""
        return quote_plus(repo)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._auth_method == AuthMethod.OAUTH:
            headers = {"Authorization": f"Bearer {self._token}"}
        else:
            headers = {"PRIVATE-TOKEN": self._token}
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    # --- Issues ---

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        resp = await self._request("POST", f"/projects/{self._project_id(repo)}/issues", json=payload)
        data = resp.json()
        return {"number": data["iid"], "html_url": data["web_url"], **data}

    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/issues/{issue_id}")
        data = resp.json()
        return {"number": data["iid"], "title": data["title"], "body": data.get("description", ""), **data}

    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        resp = await self._request(
            "POST", f"/projects/{self._project_id(repo)}/issues/{issue_id}/notes", json={"body": body}
        )
        return resp.json()

    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        issue = await self.get_issue(repo, issue_id)
        existing = issue.get("labels", [])
        all_labels = list(set(existing + labels))
        await self._request(
            "PUT", f"/projects/{self._project_id(repo)}/issues/{issue_id}", json={"labels": ",".join(all_labels)}
        )

    # --- Branches ---

    async def get_default_branch(self, repo: str) -> str:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}")
        return resp.json()["default_branch"]

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        if from_branch is None:
            from_branch = await self.get_default_branch(repo)
        resp = await self._request(
            "POST",
            f"/projects/{self._project_id(repo)}/repository/branches",
            json={"branch": branch_name, "ref": from_branch},
        )
        return resp.json()["commit"]["id"]

    # --- Files ---

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        params: dict[str, str] = {"ref": ref or await self.get_default_branch(repo)}
        encoded_path = quote_plus(path)
        resp = await self._request(
            "GET", f"/projects/{self._project_id(repo)}/repository/files/{encoded_path}", params=params
        )
        return base64.b64decode(resp.json()["content"]).decode("utf-8")

    async def list_files(self, repo: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"path": path}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/repository/tree", params=params)
        return resp.json()

    async def get_repo_tree(self, repo: str, ref: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"recursive": "true", "per_page": "100"}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/repository/tree", params=params)
        return [{"path": f["path"], "type": f["type"]} for f in resp.json()]

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        encoded_path = quote_plus(path)
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload = {"branch": branch, "content": encoded_content, "commit_message": message, "encoding": "base64"}

        # Check if file exists → update vs create
        try:
            await self.get_file_content(repo, path, ref=branch)
            resp = await self._request(
                "PUT", f"/projects/{self._project_id(repo)}/repository/files/{encoded_path}", json=payload
            )
        except httpx.HTTPStatusError:
            resp = await self._request(
                "POST", f"/projects/{self._project_id(repo)}/repository/files/{encoded_path}", json=payload
            )

        return resp.json()

    # --- Merge Requests ---

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        if base is None:
            base = await self.get_default_branch(repo)
        # GitLab marks an MR as draft via a "Draft:" title prefix.
        if draft and not title.lower().startswith(("draft:", "wip:")):
            title = f"Draft: {title}"
        resp = await self._request(
            "POST",
            f"/projects/{self._project_id(repo)}/merge_requests",
            json={
                "title": title,
                "description": body,
                "source_branch": head,
                "target_branch": base,
            },
        )
        data = resp.json()
        return {"number": data["iid"], "html_url": data["web_url"], **data}

    async def mark_pull_request_ready(self, repo: str, pr_id: int) -> dict[str, Any]:
        """Strip the ``Draft:``/``WIP:`` prefix so the MR is ready to merge."""
        mr = await self.get_pull_request(repo, pr_id)
        title = str(mr.get("title", ""))
        for prefix in ("Draft:", "draft:", "WIP:", "wip:"):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
        resp = await self._request(
            "PUT",
            f"/projects/{self._project_id(repo)}/merge_requests/{pr_id}",
            json={"title": title},
        )
        data = resp.json()
        return {"number": data.get("iid", pr_id), "is_draft": bool(data.get("draft", False))}

    async def get_pull_request(self, repo: str, pr_id: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/merge_requests/{pr_id}")
        return resp.json()

    async def get_pr_diff(self, repo: str, pr_id: int) -> str:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/merge_requests/{pr_id}/changes")
        changes = resp.json().get("changes", [])
        return "\n".join(f"--- a/{c['old_path']}\n+++ b/{c['new_path']}\n{c.get('diff', '')}" for c in changes)

    async def create_pr_review(
        self,
        repo: str,
        pr_id: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # GitLab uses notes for MR reviews
        resp = await self._request(
            "POST", f"/projects/{self._project_id(repo)}/merge_requests/{pr_id}/notes", json={"body": body}
        )
        return resp.json()

    async def merge_pull_request(self, repo: str, pr_id: int, method: str = "squash") -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if method == "squash":
            payload["squash"] = True
        resp = await self._request(
            "PUT", f"/projects/{self._project_id(repo)}/merge_requests/{pr_id}/merge", json=payload
        )
        return resp.json()

    # --- CI/CD ---

    async def get_pipeline_runs(self, repo: str, branch: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, str] = {"per_page": str(limit)}
        if branch:
            params["ref"] = branch
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/pipelines", params=params)
        return resp.json()

    async def get_pipeline_jobs(self, repo: str, run_id: int | str) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}/pipelines/{run_id}/jobs")
        return resp.json()

    # --- Repository ---

    async def get_repo_info(self, repo: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/projects/{self._project_id(repo)}")
        return resp.json()

    # --- Webhooks ---

    def verify_webhook_signature(self, body: bytes, signature: str, secret: str) -> bool:
        # GitLab uses X-Gitlab-Token header (exact match, not HMAC)
        return signature == secret

    def parse_webhook_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        obj_kind = payload.get("object_kind", "")

        if obj_kind == "issue" and payload.get("object_attributes", {}).get("action") in ("open", "update"):
            attrs = payload["object_attributes"]
            project = payload.get("project", {})
            return {
                "repo": project.get("path_with_namespace", ""),
                "issue_number": attrs.get("iid"),
                "title": attrs.get("title", ""),
                "body": attrs.get("description", "") or "",
                "labels": [lbl.get("title", "") for lbl in payload.get("labels", [])],
                "action": attrs.get("action", ""),
                "trigger_type": "gitlab_issue",
            }

        if obj_kind == "note" and payload.get("object_attributes", {}).get("noteable_type") == "Issue":
            body = payload["object_attributes"].get("note", "")
            if body.startswith("/devai"):
                issue = payload.get("issue", {})
                project = payload.get("project", {})
                return {
                    "repo": project.get("path_with_namespace", ""),
                    "issue_number": issue.get("iid"),
                    "title": issue.get("title", ""),
                    "body": issue.get("description", "") or "",
                    "labels": [lbl.get("title", "") for lbl in issue.get("labels", [])],
                    "action": "command",
                    "command": body,
                    "trigger_type": "gitlab_issue",
                }

        return None

    async def close(self) -> None:
        await self._http.aclose()
