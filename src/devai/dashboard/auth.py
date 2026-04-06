"""GitHub App OAuth flow for the dashboard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


class GitHubOAuth:
    """Handles GitHub App OAuth flow for user authorization."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=15.0)

    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        """Generate the GitHub OAuth authorization URL."""
        params = {
            "client_id": self.config.github_oauth_client_id,
            "redirect_uri": redirect_uri,
            "scope": "repo read:org project",
            "state": state,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{GITHUB_OAUTH_AUTHORIZE}?{query}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for an access token."""
        resp = await self._http.post(
            GITHUB_OAUTH_TOKEN,
            json={
                "client_id": self.config.github_oauth_client_id,
                "client_secret": self.config.github_oauth_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_user(self, access_token: str) -> dict[str, Any]:
        """Get the authenticated user's profile."""
        resp = await self._http.get(
            f"{GITHUB_API}/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_user_orgs(self, access_token: str) -> list[dict[str, Any]]:
        """Get organizations the user belongs to."""
        resp = await self._http.get(
            f"{GITHUB_API}/user/orgs",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_org_repos(self, access_token: str, org: str) -> list[dict[str, Any]]:
        """Get repositories in an organization."""
        repos = []
        page = 1
        while True:
            resp = await self._http.get(
                f"{GITHUB_API}/orgs/{org}/repos",
                params={"per_page": 100, "page": page, "sort": "updated"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend(batch)
            page += 1
            if len(batch) < 100:
                break
        return repos

    async def get_org_projects(self, access_token: str, org: str) -> list[dict[str, Any]]:
        """Get GitHub Projects v2 for an organization using GraphQL."""
        query = """
        query($org: String!) {
            organization(login: $org) {
                projectsV2(first: 50) {
                    nodes {
                        id
                        title
                        number
                        shortDescription
                        url
                        closed
                        fields(first: 20) {
                            nodes {
                                ... on ProjectV2SingleSelectField {
                                    id
                                    name
                                    options { id name }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        resp = await self._http.post(
            f"{GITHUB_API}/graphql",
            json={"query": query, "variables": {"org": org}},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        projects = data.get("data", {}).get("organization", {}).get("projectsV2", {}).get("nodes", [])
        return [p for p in projects if not p.get("closed")]

    async def create_project(self, access_token: str, org: str, title: str) -> dict[str, Any]:
        """Create a new GitHub Project v2."""
        # First get the org ID
        org_query = """
        query($org: String!) {
            organization(login: $org) { id }
        }
        """
        resp = await self._http.post(
            f"{GITHUB_API}/graphql",
            json={"query": org_query, "variables": {"org": org}},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        org_id = resp.json()["data"]["organization"]["id"]

        # Create the project
        mutation = """
        mutation($ownerId: ID!, $title: String!) {
            createProjectV2(input: { ownerId: $ownerId, title: $title }) {
                projectV2 { id title number url }
            }
        }
        """
        resp = await self._http.post(
            f"{GITHUB_API}/graphql",
            json={"query": mutation, "variables": {"ownerId": org_id, "title": title}},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["createProjectV2"]["projectV2"]

    async def get_project_items(
        self, access_token: str, org: str, project_number: int
    ) -> list[dict[str, Any]]:
        """Get all items (issues) in a GitHub Project v2."""
        query = """
        query($org: String!, $number: Int!) {
            organization(login: $org) {
                projectV2(number: $number) {
                    items(first: 100) {
                        nodes {
                            id
                            fieldValueByName(name: "Status") {
                                ... on ProjectV2ItemFieldSingleSelectValue {
                                    name
                                    optionId
                                }
                            }
                            content {
                                ... on Issue {
                                    id
                                    number
                                    title
                                    body
                                    state
                                    url
                                    labels(first: 10) {
                                        nodes { name color }
                                    }
                                    assignees(first: 5) {
                                        nodes { login avatarUrl }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        resp = await self._http.post(
            f"{GITHUB_API}/graphql",
            json={"query": query, "variables": {"org": org, "number": project_number}},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        items = (
            data.get("data", {})
            .get("organization", {})
            .get("projectV2", {})
            .get("items", {})
            .get("nodes", [])
        )
        return items

    async def move_project_item(
        self,
        access_token: str,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
    ) -> None:
        """Move a project item to a different status column."""
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
            updateProjectV2ItemFieldValue(input: {
                projectId: $projectId
                itemId: $itemId
                fieldId: $fieldId
                value: $value
            }) {
                projectV2Item { id }
            }
        }
        """
        resp = await self._http.post(
            f"{GITHUB_API}/graphql",
            json={
                "query": mutation,
                "variables": {
                    "projectId": project_id,
                    "itemId": item_id,
                    "fieldId": field_id,
                    "value": {"singleSelectOptionId": option_id},
                },
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()
