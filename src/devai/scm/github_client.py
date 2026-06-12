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
import json
import logging
import urllib.parse
from typing import Any

import httpx

from devai.scm.base import AuthMethod, SCMClient, SCMProvider
from devai.scm.github_transport import GitHubTransport

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_LABEL_MAX_LENGTH = 50


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
        org: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_method = auth_method
        self._token = token
        # Default org the repo-listing surfaces are scoped to. Lets PAT/OAuth
        # tokens query the org-scoped endpoint directly instead of walking
        # every org the token can see. Callers may override per call.
        self._org = org
        # Transport adapter: owns auth (PAT / OAuth / GitHub App installation
        # token) + the REST and GraphQL endpoint URLs. PAT and GitHub App are
        # interchangeable from here on — see github_transport.py.
        self._transport = GitHubTransport(
            self._base_url,
            use_github_app=(auth_method == AuthMethod.GITHUB_APP),
            token=token,
            app_id=app_id,
            private_key=app_private_key,
            installation_id=installation_id,
        )
        self._http = self._transport.client
        # Per-repo label cache so we don't keep POSTing labels that
        # already exist (every issue creation in a run was triggering
        # 422 already_exists for every shared label, flooding the logs).
        # Populated lazily on first call to ensure_labels(). Stores the
        # set of label names known to exist on each repo.
        self._known_labels: dict[str, set[str]] = {}

    # --- Auth (delegated to the transport adapter) ---

    async def _get_token(self) -> str:
        """Resolve the current bearer token (PAT verbatim, or a cached
        GitHub App installation token)."""
        return await self._transport.token()

    @staticmethod
    def _safe_path(path: str) -> str:
        """Percent-encode the path portion of a request URL.

        Callers build paths by interpolating repo/owner/name/ref/branch values
        (e.g. ``f"/repos/{repo}/git/ref/heads/{branch}"``). Quoting with
        ``safe="/"`` keeps the path structure (and any leading ``?query``
        already appended by the caller) intact while encoding spaces, control
        chars, and stray separators in those interpolated segments — defense in
        depth against a crafted ref/branch from reshaping the GitHub API path.
        """
        if not path:
            return path
        head, sep, query = path.partition("?")
        # Preserve already-encoded sequences (safe="%") so re-quoting is a no-op
        # on values the caller pre-encoded; keep "/" as the path separator.
        return urllib.parse.quote(head, safe="/%:@") + sep + query

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = await self._transport.auth_headers("token")
        headers.update(kwargs.pop("headers", {}))
        resp = await self._http.request(method, self._safe_path(path), headers=headers, **kwargs)
        if resp.is_client_error or resp.is_server_error:
            logger.error("GitHub API %s %s → %s: %s", method, path, resp.status_code, self._error_detail(resp))
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

    # --- Repositories ---

    async def list_installation_repos(self, per_page: int = 100, org: str = "") -> list[dict[str, Any]]:
        """List repos the configured token can see, scoped to ``org``.

        The endpoint differs by auth method:

          * GitHub App   → GET /installation/repositories  (returns {repositories:[...]};
                                                            already scoped to the installation)
          * PAT / OAuth  → GET /orgs/{org}/repos            (org-scoped — fast)
                           GET /user/repos                  (fallback when no org, or the
                                                            "org" is actually a user account)

        Why org-scope the PAT path: ``/user/repos`` returns every repo the
        token can see across *all* orgs (potentially thousands), which is slow
        enough to trip an edge gateway timeout before the request returns. When
        the platform is bound to a single org (``github_org``), asking GitHub
        for just that org's repos is one short, cheap listing. The
        ``/installation/*`` path is App-only — calling it with a PAT yields 404.

        Both endpoints return the same shape on the wire so downstream callers
        don't change. Extra fields (used by the New Pipeline Run dialog):
        owner.login, pushed_at, html_url.
        """
        org = (org or self._org).strip()
        use_token_endpoint = self._auth_method in (AuthMethod.PAT, AuthMethod.OAUTH)

        if use_token_endpoint and org:
            # Org-scoped — the common case for an org-bound platform.
            try:
                return await self._paginate_repos(
                    lambda page: (
                        f"/orgs/{org}/repos?per_page={per_page}&page={page}&type=all&sort=pushed&direction=desc"
                    ),
                    per_page,
                )
            except httpx.HTTPStatusError as e:
                # 404 → the configured "org" is a user account (or the token
                # can't see it as an org). Fall back to the user-scoped listing
                # rather than surfacing a hard error.
                if e.response.status_code != 404:
                    raise
                logger.info("'/orgs/%s/repos' 404 — falling back to /user/repos", org)

        if use_token_endpoint:
            repos = await self._paginate_repos(
                lambda page: (
                    f"/user/repos"
                    f"?per_page={per_page}&page={page}&sort=pushed&direction=desc"
                    "&affiliation=owner,collaborator,organization_member"
                ),
                per_page,
            )
            # A broad token spans orgs — keep only the bound org's repos when set.
            if org:
                repos = [r for r in repos if (r.get("owner") or {}).get("login", "").lower() == org.lower()]
            return repos

        # GitHub App installation token — already scoped to the installation.
        return await self._paginate_repos(
            lambda page: f"/installation/repositories?per_page={per_page}&page={page}",
            per_page,
            envelope="repositories",
        )

    async def _paginate_repos(
        self,
        path_for_page: Any,
        per_page: int,
        envelope: str = "",
    ) -> list[dict[str, Any]]:
        """Page through a GitHub repo-listing endpoint into the common shape.

        ``path_for_page(page)`` builds the request path for a 1-based page.
        ``envelope`` names the wrapping key when the response is an object
        (``{"repositories": [...]}``) rather than a bare array.
        """
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._request("GET", path_for_page(page))
            payload = resp.json()
            batch = payload.get(envelope, []) if envelope else payload
            for r in batch:
                repos.append(
                    {
                        "full_name": r["full_name"],
                        "name": r["name"],
                        "owner": r.get("owner") or {},
                        "description": r.get("description") or "",
                        "language": r.get("language") or "",
                        "private": r.get("private", False),
                        "default_branch": r.get("default_branch", "main"),
                        "html_url": r.get("html_url", ""),
                        "pushed_at": r.get("pushed_at", ""),
                    }
                )
            if len(batch) < per_page:
                break
            page += 1
            if page > 10:  # hard cap — 1000 repos is enough for a picker
                break
        return repos

    async def create_repo(
        self,
        org: str,
        name: str,
        description: str = "",
        private: bool = True,
        auto_init: bool = True,
    ) -> dict[str, Any]:
        """Create a new repository in an organization.

        ``auto_init=False`` creates an empty repo (no initial commit); the
        first contents write then creates the default branch. The scaffold
        flow uses that so it can seed its own README without colliding with an
        auto-generated one."""
        resp = await self._request(
            "POST",
            f"/orgs/{org}/repos",
            json={"name": name, "description": description, "private": private, "auto_init": auto_init},
        )
        data = resp.json()
        return {
            "full_name": data["full_name"],
            "name": data["name"],
            "description": data.get("description") or "",
            "html_url": data["html_url"],
            "default_branch": data.get("default_branch", "main"),
        }

    async def set_branch_protection(
        self,
        repo: str,
        branch: str,
        *,
        required_approvals: int = 1,
    ) -> dict[str, Any]:
        """Enable a baseline branch-protection quality gate on ``branch``.

        Requires a PR with the given number of approvals, dismisses stale
        approvals, requires conversation resolution, and blocks force-pushes
        and deletions. Callers should treat this as best-effort — the GitHub
        App needs admin on the repo, and protection can't be set on every plan
        (e.g. private repos on a free org)."""
        body = {
            "required_status_checks": None,
            "enforce_admins": False,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "required_approving_review_count": max(0, required_approvals),
            },
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "required_conversation_resolution": True,
        }
        resp = await self._request("PUT", f"/repos/{repo}/branches/{branch}/protection", json=body)
        return resp.json()

    # --- Issues ---

    async def _prime_label_cache(self, repo: str) -> None:
        """Pull every existing label on the repo (paginated) into the cache.

        Done once per repo per process. Subsequent ensure_labels() calls
        only POST when the label is genuinely missing — no more 422
        already_exists spam in the pod logs.
        """
        known: set[str] = set()
        page = 1
        try:
            while True:
                resp = await self._request(
                    "GET",
                    f"/repos/{repo}/labels",
                    params={"per_page": "100", "page": str(page)},
                )
                items = resp.json()
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    name = item.get("name") if isinstance(item, dict) else None
                    if name:
                        known.add(name)
                if len(items) < 100:
                    break
                page += 1
                if page > 10:  # safety cap (1000 labels is plenty)
                    break
        except Exception as e:
            logger.debug("Could not prime label cache for %s: %s", repo, e)

        self._known_labels[repo] = known
        logger.info("Label cache primed for %s — %d existing labels", repo, len(known))

    async def ensure_labels(self, repo: str, labels: list[str] | None) -> list[str]:
        """Create labels on a repo if they don't already exist.

        Uses an in-process cache of labels-known-to-exist so we don't
        re-POST labels that already exist on every single issue create
        (which used to flood the logs with 422 already_exists warnings).
        """
        usable_labels: list[str] = []
        normalized = self._normalize_labels(labels)
        if not normalized:
            return usable_labels

        # Lazy-prime the cache on first use for this repo
        if repo not in self._known_labels:
            await self._prime_label_cache(repo)

        cache = self._known_labels.setdefault(repo, set())

        for label in normalized:
            if label in cache:
                # Already known to exist — skip the POST entirely
                usable_labels.append(label)
                continue

            try:
                await self._request(
                    "POST",
                    f"/repos/{repo}/labels",
                    json={"name": label, "color": "6366f1"},
                )
                cache.add(label)
                usable_labels.append(label)
            except httpx.HTTPStatusError as exc:
                response = exc.response
                detail = self._error_detail(response) if response is not None else str(exc)
                if response is not None and response.status_code == 422 and "already_exists" in detail:
                    # Race: someone else created it between cache prime
                    # and our POST. Add to cache and continue silently.
                    cache.add(label)
                    usable_labels.append(label)
                    continue
                logger.warning("Skipping invalid GitHub label %r on %s: %s", label, repo, detail)
                continue

        return usable_labels

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        # GitHub requires a non-empty title
        safe_title = (title or "").strip() or "Untitled Issue"
        safe_body = (body or "").strip() or ""
        safe_labels = await self.ensure_labels(repo, labels)
        payload: dict[str, Any] = {"title": safe_title, "body": safe_body}
        if safe_labels:
            payload["labels"] = safe_labels
        try:
            resp = await self._request("POST", f"/repos/{repo}/issues", json=payload)
            return resp.json()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            detail = self._error_detail(response) if response is not None else str(exc)
            label_error = response is not None and response.status_code == 422 and "label" in detail.lower()
            if label_error and "labels" in payload:
                logger.warning("Retrying GitHub issue creation on %s without labels after 422: %s", repo, detail)
                retry_resp = await self._request(
                    "POST", f"/repos/{repo}/issues", json={"title": safe_title, "body": safe_body}
                )
                return retry_resp.json()
            raise RuntimeError(
                f"Failed to create issue on {repo}: {detail}. Check that the repo exists and has Issues enabled."
            ) from exc

    async def get_issue(self, repo: str, issue_id: int | str) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo}/issues/{issue_id}")
        return resp.json()

    async def list_issues(
        self,
        repo: str,
        state: str = "open",
        labels: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List issues on a GitHub repo.

        Filters out pull requests (GitHub returns PRs as issues from this
        endpoint). Used by the cross-run dedup logic in the SCMClient base.
        """
        params: dict[str, str] = {"state": state, "per_page": str(min(limit, 100))}
        if labels:
            params["labels"] = ",".join(labels)
        try:
            resp = await self._request("GET", f"/repos/{repo}/issues", params=params)
            data = resp.json()
        except Exception as e:
            logger.warning("Failed to list issues on %s: %s", repo, e)
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if "pull_request" not in item]

    async def add_comment(self, repo: str, issue_id: int | str, body: str) -> dict[str, Any]:
        resp = await self._request("POST", f"/repos/{repo}/issues/{issue_id}/comments", json={"body": body})
        return resp.json()

    async def add_labels(self, repo: str, issue_id: int | str, labels: list[str]) -> None:
        safe_labels = await self.ensure_labels(repo, labels)
        if not safe_labels:
            return
        await self._request("POST", f"/repos/{repo}/issues/{issue_id}/labels", json={"labels": safe_labels})

    async def update_issue(
        self,
        repo: str,
        issue_id: int | str,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if not payload:
            return await self.get_issue(repo, issue_id)
        resp = await self._request("PATCH", f"/repos/{repo}/issues/{issue_id}", json=payload)
        return resp.json()

    # --- Branches ---

    async def get_default_branch(self, repo: str) -> str:
        resp = await self._request("GET", f"/repos/{repo}")
        return resp.json()["default_branch"]

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        if from_branch is None:
            from_branch = await self.get_default_branch(repo)
        sha_resp = await self._request("GET", f"/repos/{repo}/git/ref/heads/{from_branch}")
        sha = sha_resp.json()["object"]["sha"]
        try:
            await self._request(
                "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch_name}", "sha": sha}
            )
        except Exception as e:  # noqa: BLE001
            # 422 "Reference already exists" — the branch is THERE; agents
            # re-running a stage (resume/retry) hit this constantly. Return
            # the existing branch head instead of failing the tool call.
            if "422" not in str(e):
                raise
            existing = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch_name}")
            return existing.json()["object"]["sha"]
        return sha

    # --- Files ---

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        params: dict[str, str] = {}
        if ref:
            params["ref"] = ref
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params=params)
        data = resp.json()
        if isinstance(data, list):
            # The path is a DIRECTORY — indexing the list with "content"
            # raised `list indices must be integers` deep in the tool layer.
            names = ", ".join(str(item.get("name", "")) for item in data[:15])
            raise FileNotFoundError(
                f"{path!r} is a directory, not a file — it contains: {names}. "
                "Use scm_list_files to browse it."
            )
        return base64.b64decode(data["content"]).decode("utf-8")

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
        try:
            resp = await self._request("PUT", f"/repos/{repo}/contents/{path}", json=payload)
        except Exception as e:  # noqa: BLE001
            # UPDATING an existing file requires its current blob sha; agents
            # never pass one, so every update 422'd ("sha wasn't supplied")
            # and they burned turns fighting it. Fetch the sha and retry once
            # — the "or update" half of this method's contract.
            if "422" not in str(e) or sha:
                raise
            # The lookup is best-effort: if the 422 had another cause (bad
            # path, path is a DIRECTORY — the GET then returns a list, or
            # the file truly doesn't exist → 404) we must re-raise the
            # ORIGINAL 422, never a masking secondary error.
            try:
                lookup = await self._request(
                    "GET", f"/repos/{repo}/contents/{path}", params={"ref": branch}
                )
                body = lookup.json()
                current_sha = body.get("sha") if isinstance(body, dict) else None
            except Exception:  # noqa: BLE001
                current_sha = None
            if not current_sha:
                raise
            payload["sha"] = current_sha
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
        draft: bool = False,
    ) -> dict[str, Any]:
        if base is None:
            base = await self.get_default_branch(repo)
        resp = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )
        return resp.json()

    async def mark_pull_request_ready(self, repo: str, pr_id: int) -> dict[str, Any]:
        """Promote a draft PR to ready-for-review.

        GitHub's REST API can't flip draft → ready, so this resolves the
        PR's GraphQL node id and runs the ``markPullRequestReadyForReview``
        mutation. Idempotent on the server side — a non-draft PR is left
        unchanged. Returns the PR's id/number/isDraft post-state.
        """
        owner, name = repo.split("/", 1)
        node = await self._graphql(
            """
            query($owner: String!, $name: String!, $number: Int!) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: $number) { id }
                }
            }
            """,
            {"owner": owner, "name": name, "number": pr_id},
        )
        pr_node_id = (node.get("repository", {}).get("pullRequest", {}) or {}).get("id", "")
        if not pr_node_id:
            raise ValueError(f"PR #{pr_id} not found in {repo}")
        result = await self._graphql(
            """
            mutation($id: ID!) {
                markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                    pullRequest { number isDraft }
                }
            }
            """,
            {"id": pr_node_id},
        )
        pr = result.get("markPullRequestReadyForReview", {}).get("pullRequest", {}) or {}
        return {"number": pr.get("number", pr_id), "is_draft": pr.get("isDraft", False)}

    async def get_pull_request(self, repo: str, pr_id: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{repo}/pulls/{pr_id}")
        return resp.json()

    async def get_pr_diff(self, repo: str, pr_id: int) -> str:
        headers = await self._transport.auth_headers("token")
        headers["Accept"] = "application/vnd.github.diff"
        resp = await self._http.get(self._safe_path(f"/repos/{repo}/pulls/{pr_id}"), headers=headers)
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
        try:
            resp = await self._request("POST", f"/repos/{repo}/pulls/{pr_id}/reviews", json=payload)
        except Exception as e:  # noqa: BLE001
            # GitHub 422s APPROVE / REQUEST_CHANGES on your OWN pull request —
            # and our bot authors the PRs its reviewer agents then review.
            # Degrade to a COMMENT review carrying the verdict in the body so
            # the review lands instead of burning agent turns on workarounds.
            if "422" not in str(e) or event.upper() == "COMMENT":
                raise
            payload["event"] = "COMMENT"
            payload["body"] = f"**[Review verdict: {event.upper()}]**\n\n{body}"
            resp = await self._request("POST", f"/repos/{repo}/pulls/{pr_id}/reviews", json=payload)
        return resp.json()

    async def merge_pull_request(self, repo: str, pr_id: int, method: str = "squash") -> dict[str, Any]:
        resp = await self._request("PUT", f"/repos/{repo}/pulls/{pr_id}/merge", json={"merge_method": method})
        return resp.json()

    async def request_reviewers(self, repo: str, pr_id: int, reviewers: list[str]) -> dict[str, Any]:
        """Request reviewers on a PR. Splits team handles (``org/team``)
        from individual logins, matching GitHub's two-list payload."""
        users: list[str] = []
        teams: list[str] = []
        for r in reviewers:
            r = (r or "").strip().lstrip("@")
            if not r:
                continue
            if "/" in r:
                teams.append(r.split("/", 1)[1])
            else:
                users.append(r)
        payload: dict[str, Any] = {}
        if users:
            payload["reviewers"] = users
        if teams:
            payload["team_reviewers"] = teams
        resp = await self._request("POST", f"/repos/{repo}/pulls/{pr_id}/requested_reviewers", json=payload)
        data = resp.json()
        return {"requested": [u.get("login") for u in data.get("requested_reviewers", [])]}

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
        """Execute a GitHub GraphQL query.

        Endpoint is derived from the configured base URL (github.com or
        Enterprise) and auth comes from the same credential as REST — so
        GraphQL works under both PAT and GitHub App.
        """
        headers = await self._transport.auth_headers("bearer")
        resp = await self._http.post(
            self._transport.graphql_url,
            headers=headers,
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data.get("data", {})

    async def probe_markers(self, repos: list[tuple[str, str, str]], marker_path: str) -> dict[str, bool]:
        """Batch-probe marker presence across many repos in one GraphQL call.

        Aliases each repo as ``r0, r1, …`` and asks for
        ``object(expression: "<ref>:<marker_path>")`` — a non-null object
        means the file exists on that ref. Folds what would be N REST
        ``GET /contents`` calls into a single round trip (chunked at 50 to
        stay well under GraphQL node limits). This is what keeps the Repos
        page's onboarding-status column fast enough to cache.
        """
        out: dict[str, bool] = {}
        chunk = 50
        for start in range(0, len(repos), chunk):
            batch = repos[start : start + chunk]
            alias_map: dict[str, str] = {}
            fields: list[str] = []
            variables: dict[str, Any] = {}
            for i, (owner, name, ref) in enumerate(batch):
                alias = f"r{i}"
                alias_map[alias] = f"{owner}/{name}"
                ov, nv, ev = f"o{i}", f"n{i}", f"e{i}"
                variables[ov] = owner
                variables[nv] = name
                variables[ev] = f"{ref or 'HEAD'}:{marker_path}"
                fields.append(
                    f"{alias}: repository(owner: ${ov}, name: ${nv}) {{ object(expression: ${ev}) {{ __typename }} }}"
                )
            var_decls = ", ".join(f"${k}: String!" for k in variables)
            query = f"query({var_decls}) {{ {' '.join(fields)} }}"
            try:
                data = await self._graphql(query, variables)
            except Exception as e:  # noqa: BLE001 — one bad batch shouldn't blank the page
                # Leave failed repos OUT of the result (don't assert False):
                # an "unknown" verdict must never be read as "marker gone",
                # or a transient GraphQL error would offload onboarded repos
                # to DORMANT on the next reconcile. Callers treat a missing
                # key as "no change".
                logger.warning("probe_markers GraphQL batch failed: %s", e)
                continue
            for alias, full in alias_map.items():
                node = data.get(alias) or {}
                out[full] = bool(node.get("object"))
        return out

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
