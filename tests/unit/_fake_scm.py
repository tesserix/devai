"""A minimal fake SCMClient used by the onboarding service + route tests.

Records the calls that matter and lets a test declare which repos exist,
which already carry the marker, and what the default branch is.
"""

from __future__ import annotations

from typing import Any


class FakeSCM:
    def __init__(
        self,
        repos: list[dict[str, Any]] | None = None,
        markers: set[str] | None = None,
        default_branch: str = "main",
    ) -> None:
        self.repos = repos or []
        self.markers = set(markers or set())   # full_names with a marker present
        self.default_branch = default_branch
        self.calls: list[tuple[str, tuple, dict]] = []
        self._pr_seq = 100

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    async def list_installation_repos(self, per_page: int = 100, org: str = "") -> list[dict[str, Any]]:
        self._record("list_installation_repos", per_page=per_page, org=org)
        return self.repos

    async def probe_markers(self, repos, marker_path):
        self._record("probe_markers", repos, marker_path)
        return {f"{o}/{n}": f"{o}/{n}" in self.markers for o, n, _ref in repos}

    async def get_default_branch(self, repo: str) -> str:
        return self.default_branch

    async def get_file_content(self, repo: str, path: str, ref: str | None = None) -> str:
        if repo in self.markers:
            return "apiVersion: devai.tesserix.app/v1alpha1\nkind: PlatformConfig\n"
        raise RuntimeError("404 not found")

    async def create_branch(self, repo: str, branch_name: str, from_branch: str | None = None) -> str:
        self._record("create_branch", repo=repo, branch_name=branch_name, from_branch=from_branch)
        return "sha123"

    async def create_or_update_file(self, repo, path, content, message, branch, sha=None):
        self._record("create_or_update_file", repo=repo, path=path, branch=branch)
        return {"content": {"path": path}}

    async def create_pull_request(self, repo, title, body, head, base=None, draft=False):
        self._record("create_pull_request", repo=repo, head=head, base=base, title=title, draft=draft)
        self._pr_seq += 1
        return {"number": self._pr_seq, "html_url": f"https://github.com/{repo}/pull/{self._pr_seq}"}

    async def mark_pull_request_ready(self, repo, pr_id):
        self._record("mark_pull_request_ready", repo=repo, pr_id=pr_id)
        return {"number": pr_id, "is_draft": False}

    async def merge_pull_request(self, repo, pr_id, method="squash"):
        self._record("merge_pull_request", repo=repo, pr_id=pr_id, method=method)
        return {"merged": True}

    async def request_reviewers(self, repo, pr_id, reviewers):
        self._record("request_reviewers", repo=repo, pr_id=pr_id, reviewers=reviewers)
        return {"requested": reviewers}

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)
