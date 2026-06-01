"""Azure DevOps SCM client — supports PAT auth.

Auth:
  ADO uses Basic Auth with PAT: Authorization: Basic base64(":PAT")
  Config: scm_token = the PAT

Repo format: "org/project/repo"
ADO API: https://dev.azure.com/{org}/{project}/_apis/

Work Items replace Issues.
Pull Requests work similarly to GitHub.
Pipelines replace GitHub Actions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

import httpx

from devai.scm.base import SCMClient, SCMProvider

logger = logging.getLogger(__name__)


class AzureDevOpsSCMClient(SCMClient):
    """Azure DevOps implementation of the SCM interface."""

    provider = SCMProvider.AZURE_DEVOPS

    def __init__(
        self,
        base_url: str = "https://dev.azure.com",
        token: str = "",
        organization: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._org = organization
        # ADO uses Basic auth with PAT
        auth_str = base64.b64encode(f":{token}".encode()).decode()
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json",
            },
        )

    def _parse_repo(self, repo: str) -> tuple[str, str, str]:
        """Parse 'org/project/repo' into (org, project, repo_name)."""
        parts = repo.split("/")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return self._org, parts[0], parts[1]
        raise ValueError(f"Invalid ADO repo format: {repo}. Expected 'org/project/repo'")

    def _api_url(self, org: str, project: str, path: str) -> str:
        return f"{self._base_url}/{org}/{project}/_apis/{path}"

    def _git_url(self, org: str, project: str, repo_name: str, path: str) -> str:
        return f"{self._base_url}/{org}/{project}/_apis/git/repositories/{repo_name}/{path}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # ADO requires api-version parameter
        params = kwargs.pop("params", {})
        params.setdefault("api-version", "7.1")
        resp = await self._http.request(method, url, params=params, **kwargs)
        resp.raise_for_status()
        return resp

    # --- Work Items (Issues) ---

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        org, project, _ = self._parse_repo(repo)
        # ADO uses JSON Patch for work item creation
        patch_doc = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": body},
        ]
        if labels:
            patch_doc.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(labels)})

        url = self._api_url(org, project, "wit/workitems/$User Story")
        resp = await self._http.post(
            url,
            json=patch_doc,
            params={"api-version": "7.1"},
            headers={
                **self._http.headers,
                "Content-Type": "application/json-patch+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["id"], "html_url": data.get("_links", {}).get("html", {}).get("href", ""), **data}

    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        org, project, _ = self._parse_repo(repo)
        url = self._api_url(org, project, f"wit/workitems/{issue_id}")
        resp = await self._request("GET", url, params={"$expand": "All"})
        data = resp.json()
        fields = data.get("fields", {})
        return {
            "number": data["id"],
            "title": fields.get("System.Title", ""),
            "body": fields.get("System.Description", ""),
            "labels": fields.get("System.Tags", "").split("; ") if fields.get("System.Tags") else [],
            **data,
        }

    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        org, project, _ = self._parse_repo(repo)
        url = self._api_url(org, project, f"wit/workitems/{issue_id}/comments")
        resp = await self._request("POST", url, json={"text": body})
        return resp.json()

    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        org, project, _ = self._parse_repo(repo)
        # Get existing tags, append new ones
        item = await self.get_issue(repo, issue_id)
        existing = item.get("labels", [])
        all_tags = list(set(existing + labels))
        patch_doc = [{"op": "replace", "path": "/fields/System.Tags", "value": "; ".join(all_tags)}]
        url = self._api_url(org, project, f"wit/workitems/{issue_id}")
        await self._http.patch(
            url,
            json=patch_doc,
            params={"api-version": "7.1"},
            headers={**self._http.headers, "Content-Type": "application/json-patch+json"},
        )

    # --- Branches ---

    async def get_default_branch(self, repo: str) -> str:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, "")
        # Remove trailing slash
        url = url.rstrip("/")
        resp = await self._request("GET", url)
        branch = resp.json().get("defaultBranch", "refs/heads/main")
        return branch.replace("refs/heads/", "")

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        org, project, repo_name = self._parse_repo(repo)
        if from_branch is None:
            from_branch = await self.get_default_branch(repo)
        # Get source branch SHA
        refs_url = self._git_url(org, project, repo_name, f"refs?filter=heads/{from_branch}")
        resp = await self._request("GET", refs_url)
        refs = resp.json().get("value", [])
        if not refs:
            raise ValueError(f"Branch '{from_branch}' not found")
        sha = refs[0]["objectId"]

        # Create the branch
        push_url = self._git_url(org, project, repo_name, "refs")
        await self._request(
            "POST",
            push_url,
            json=[
                {
                    "name": f"refs/heads/{branch_name}",
                    "oldObjectId": "0000000000000000000000000000000000000000",
                    "newObjectId": sha,
                }
            ],
        )
        return sha

    # --- Files ---

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        org, project, repo_name = self._parse_repo(repo)
        params: dict[str, str] = {"path": path}
        if ref:
            params["versionDescriptor.version"] = ref
            params["versionDescriptor.versionType"] = "branch"
        url = self._git_url(org, project, repo_name, "items")
        resp = await self._request("GET", url, params=params)
        return resp.text

    async def list_files(self, repo: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        org, project, repo_name = self._parse_repo(repo)
        params: dict[str, str] = {"scopePath": path or "/", "recursionLevel": "oneLevel"}
        if ref:
            params["versionDescriptor.version"] = ref
        url = self._git_url(org, project, repo_name, "items")
        resp = await self._request("GET", url, params=params)
        return resp.json().get("value", [])

    async def get_repo_tree(self, repo: str, ref: str | None = None) -> list[dict[str, Any]]:
        org, project, repo_name = self._parse_repo(repo)
        params: dict[str, str] = {"recursionLevel": "full"}
        if ref:
            params["versionDescriptor.version"] = ref
        url = self._git_url(org, project, repo_name, "items")
        resp = await self._request("GET", url, params=params)
        items = resp.json().get("value", [])
        return [{"path": i["path"], "type": "blob" if not i.get("isFolder") else "tree"} for i in items]

    async def create_or_update_file(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        encoded = base64.b64encode(content.encode()).decode()
        # ADO uses push API for file changes
        push_url = self._git_url(org, project, repo_name, "pushes")
        change_type = "edit" if sha else "add"
        payload = {
            "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": sha or "0" * 40}],
            "commits": [
                {
                    "comment": message,
                    "changes": [
                        {
                            "changeType": change_type,
                            "item": {"path": path},
                            "newContent": {"content": encoded, "contentType": "base64encoded"},
                        }
                    ],
                }
            ],
        }
        resp = await self._request("POST", push_url, json=payload)
        return resp.json()

    # --- Pull Requests ---

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        if base is None:
            base = await self.get_default_branch(repo)
        url = self._git_url(org, project, repo_name, "pullrequests")
        resp = await self._request(
            "POST",
            url,
            json={
                "title": title,
                "description": body,
                "sourceRefName": f"refs/heads/{head}",
                "targetRefName": f"refs/heads/{base}",
                "isDraft": draft,
            },
        )
        data = resp.json()
        return {"number": data["pullRequestId"], **data}

    async def mark_pull_request_ready(self, repo: str, pr_id: int) -> dict[str, Any]:
        """Clear ``isDraft`` on an ADO pull request so it becomes a real PR."""
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}")
        resp = await self._request("PATCH", url, json={"isDraft": False})
        data = resp.json()
        return {"number": data.get("pullRequestId", pr_id), "is_draft": bool(data.get("isDraft", False))}

    async def get_pull_request(self, repo: str, pr_id: int) -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}")
        resp = await self._request("GET", url)
        return resp.json()

    async def get_pr_diff(self, repo: str, pr_id: int) -> str:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}/iterations")
        resp = await self._request("GET", url)
        iterations = resp.json().get("value", [])
        if not iterations:
            return ""
        last_iter = iterations[-1]["id"]
        changes_url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}/iterations/{last_iter}/changes")
        changes_resp = await self._request("GET", changes_url)
        entries = changes_resp.json().get("changeEntries", [])
        return "\n".join(f"{e.get('changeType', '?')}: {e['item']['path']}" for e in entries)

    async def create_pr_review(
        self,
        repo: str,
        pr_id: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}/threads")
        resp = await self._request(
            "POST",
            url,
            json={
                "comments": [{"content": body, "commentType": "text"}],
                "status": "active",
            },
        )
        return resp.json()

    async def merge_pull_request(self, repo: str, pr_id: int, method: str = "squash") -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, f"pullrequests/{pr_id}")
        resp = await self._request(
            "PATCH",
            url,
            json={
                "status": "completed",
                "completionOptions": {"mergeStrategy": "squash" if method == "squash" else "noFastForward"},
            },
        )
        return resp.json()

    # --- CI/CD (Pipelines) ---

    async def get_pipeline_runs(self, repo: str, branch: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        org, project, _ = self._parse_repo(repo)
        url = self._api_url(org, project, "build/builds")
        params: dict[str, str] = {"$top": str(limit)}
        if branch:
            params["branchName"] = f"refs/heads/{branch}"
        resp = await self._request("GET", url, params=params)
        return resp.json().get("value", [])

    async def get_pipeline_jobs(self, repo: str, run_id: int | str) -> list[dict[str, Any]]:
        org, project, _ = self._parse_repo(repo)
        url = self._api_url(org, project, f"build/builds/{run_id}/timeline")
        resp = await self._request("GET", url)
        records = resp.json().get("records", [])
        return [r for r in records if r.get("type") == "Task"]

    # --- Repository ---

    async def get_repo_info(self, repo: str) -> dict[str, Any]:
        org, project, repo_name = self._parse_repo(repo)
        url = self._git_url(org, project, repo_name, "")
        resp = await self._request("GET", url.rstrip("/"))
        return resp.json()

    # --- Webhooks ---

    def verify_webhook_signature(self, body: bytes, signature: str, secret: str) -> bool:
        # ADO service hooks use a configurable shared secret signed as
        # HMAC-SHA256 over the body. Fail closed: a missing secret or
        # signature means we cannot authenticate the delivery, so reject
        # it rather than accept every unauthenticated request.
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        event_type_ado = payload.get("eventType", "")

        if event_type_ado == "workitem.created" or event_type_ado == "workitem.updated":
            resource = payload.get("resource", {})
            fields = resource.get("fields", {})
            return {
                "repo": payload.get("resourceContainers", {}).get("project", {}).get("id", ""),
                "issue_number": resource.get("id"),
                "title": fields.get("System.Title", ""),
                "body": fields.get("System.Description", "") or "",
                "labels": fields.get("System.Tags", "").split("; ") if fields.get("System.Tags") else [],
                "action": "created" if "created" in event_type_ado else "updated",
                "trigger_type": "ado_workitem",
            }

        return None

    async def close(self) -> None:
        await self._http.aclose()
