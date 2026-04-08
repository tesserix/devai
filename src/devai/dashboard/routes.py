"""Dashboard API routes — OAuth, projects, board, pipeline status, governance."""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from devai.dashboard.auth import GitHubOAuth
from devai.dashboard.keycloak_auth import KeycloakOIDC
from devai.dashboard.templates import INDEX_HTML

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

STATIC_DIR = Path(__file__).parent / "static"


# --- Dashboard Page ---


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_page() -> str:
    """Serve the dashboard SPA."""
    return INDEX_HTML


# --- Auth (supports Keycloak OIDC or GitHub OAuth) ---


@router.get("/auth/login")
async def auth_login(request: Request) -> RedirectResponse:
    """Redirect to the configured auth provider (Keycloak or GitHub)."""
    config = request.app.state.config
    state = secrets.token_urlsafe(32)
    redirect_uri = f"{config.dashboard_base_url}/dashboard/auth/callback"

    redis = request.app.state.state_manager.redis
    await redis.set(f"devai:oauth:state:{state}", "1", ex=600)

    if config.auth_provider == "keycloak":
        kc = KeycloakOIDC(config)
        url = kc.get_authorize_url(redirect_uri, state)
    else:
        oauth = GitHubOAuth(config)
        url = oauth.get_authorize_url(redirect_uri, state)

    return RedirectResponse(url)


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
    """Handle auth callback from Keycloak or GitHub."""
    config = request.app.state.config
    redis = request.app.state.state_manager.redis

    # Verify state
    stored = await redis.get(f"devai:oauth:state:{state}")
    if not stored:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    await redis.delete(f"devai:oauth:state:{state}")

    redirect_uri = f"{config.dashboard_base_url}/dashboard/auth/callback"

    if config.auth_provider == "keycloak":
        kc = KeycloakOIDC(config)
        token_data = await kc.exchange_code(code, redirect_uri)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token")

        userinfo = await kc.get_userinfo(access_token)
        await kc.close()

        session_id = secrets.token_urlsafe(48)
        session_data = json.dumps(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "auth_provider": "keycloak",
                "user_login": userinfo.get("preferred_username", userinfo.get("sub", "")),
                "user_name": userinfo.get("name", ""),
                "user_email": userinfo.get("email", ""),
                "avatar_url": "",
                "roles": userinfo.get("realm_access", {}).get("roles", []),
            }
        )
    else:
        oauth = GitHubOAuth(config)
        token_data = await oauth.exchange_code(code)
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token")

        user = await oauth.get_user(access_token)
        await oauth.close()

        session_id = secrets.token_urlsafe(48)
        session_data = json.dumps(
            {
                "access_token": access_token,
                "refresh_token": "",
                "auth_provider": "github",
                "user_login": user["login"],
                "user_name": user.get("name", ""),
                "user_email": user.get("email", ""),
                "avatar_url": user.get("avatar_url", ""),
                "roles": [],
            }
        )

    await redis.set(f"devai:session:{session_id}", session_data, ex=86400)

    response = RedirectResponse("/dashboard")
    response.set_cookie("devai_session", session_id, httponly=True, secure=True, samesite="lax", max_age=86400)
    return response


@router.get("/auth/logout")
async def oauth_logout(request: Request) -> RedirectResponse:
    """Clear the session."""
    session_id = request.cookies.get("devai_session")
    if session_id:
        redis = request.app.state.state_manager.redis
        await redis.delete(f"devai:session:{session_id}")
    response = RedirectResponse("/dashboard")
    response.delete_cookie("devai_session")
    return response


async def _get_session(request: Request) -> dict[str, Any] | None:
    """Get the current user session from cookie."""
    session_id = request.cookies.get("devai_session")
    if not session_id:
        return None
    redis = request.app.state.state_manager.redis
    data = await redis.get(f"devai:session:{session_id}")
    if not data:
        return None
    return json.loads(data)


# --- API Endpoints ---


@router.get("/api/me")
async def get_current_user(request: Request) -> dict[str, Any]:
    """Get current authenticated user."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "login": session["user_login"],
        "name": session["user_name"],
        "avatar_url": session["avatar_url"],
    }


@router.get("/api/orgs")
async def get_orgs(request: Request) -> list[dict[str, Any]]:
    """Get user's GitHub organizations."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    oauth = GitHubOAuth(request.app.state.config)
    orgs = await oauth.get_user_orgs(session["access_token"])
    await oauth.close()
    return [{"login": o["login"], "avatar_url": o.get("avatar_url", "")} for o in orgs]


@router.get("/api/orgs/{org}/repos")
async def get_repos(request: Request, org: str) -> list[dict[str, Any]]:
    """Get repositories in an organization."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    oauth = GitHubOAuth(request.app.state.config)
    repos = await oauth.get_org_repos(session["access_token"], org)
    await oauth.close()
    return [
        {
            "full_name": r["full_name"],
            "name": r["name"],
            "description": r.get("description", ""),
            "language": r.get("language", ""),
        }
        for r in repos
    ]


@router.get("/api/orgs/{org}/projects")
async def get_projects(request: Request, org: str) -> list[dict[str, Any]]:
    """Get GitHub Projects v2 for an organization."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    oauth = GitHubOAuth(request.app.state.config)
    projects = await oauth.get_org_projects(session["access_token"], org)
    await oauth.close()
    return projects


@router.post("/api/orgs/{org}/projects")
async def create_project(request: Request, org: str) -> dict[str, Any]:
    """Create a new GitHub Project v2."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()
    title = body.get("title", "DevAI Pipeline Board")
    oauth = GitHubOAuth(request.app.state.config)
    project = await oauth.create_project(session["access_token"], org, title)
    await oauth.close()
    return project


@router.get("/api/orgs/{org}/projects/{project_number}/board")
async def get_board(request: Request, org: str, project_number: int) -> dict[str, Any]:
    """Get Kanban board data — items grouped by status column."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    oauth = GitHubOAuth(request.app.state.config)
    items = await oauth.get_project_items(session["access_token"], org, project_number)
    await oauth.close()

    # Group items by status column
    columns: dict[str, list[dict[str, Any]]] = {
        "Backlog": [],
        "Ready": [],
        "In Progress": [],
        "Done": [],
    }

    for item in items:
        content = item.get("content")
        if not content or not content.get("number"):
            continue

        status_field = item.get("fieldValueByName")
        status_name = status_field.get("name", "Backlog") if status_field else "Backlog"

        # Map to our columns
        if status_name not in columns:
            status_name = "Backlog"

        card = {
            "item_id": item["id"],
            "issue_number": content["number"],
            "title": content["title"],
            "body": (content.get("body") or "")[:200],
            "state": content["state"],
            "url": content["url"],
            "labels": [lbl["name"] for lbl in content.get("labels", {}).get("nodes", [])],
            "assignees": [a["login"] for a in content.get("assignees", {}).get("nodes", [])],
        }
        columns[status_name].append(card)

    return {"columns": columns}


@router.post("/api/orgs/{org}/projects/{project_number}/move")
async def move_item(request: Request, org: str, project_number: int) -> dict[str, str]:
    """Move a project item to a different status column."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()

    oauth = GitHubOAuth(request.app.state.config)
    # Get project fields to find the Status field and option IDs
    projects = await oauth.get_org_projects(session["access_token"], org)
    project = next((p for p in projects if p["number"] == project_number), None)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Find the Status field
    status_field = None
    for field in project.get("fields", {}).get("nodes", []):
        if field.get("name") == "Status":
            status_field = field
            break

    if not status_field:
        raise HTTPException(status_code=400, detail="No Status field found in project")

    # Find the target option
    target_status = body["target_status"]
    option = next((o for o in status_field["options"] if o["name"] == target_status), None)
    if not option:
        raise HTTPException(status_code=400, detail=f"Status '{target_status}' not found")

    await oauth.move_project_item(
        session["access_token"],
        project["id"],
        body["item_id"],
        status_field["id"],
        option["id"],
    )
    await oauth.close()

    return {"status": "moved"}


@router.get("/api/repos")
async def list_repos(request: Request) -> list[dict[str, Any]]:
    """List repositories accessible to the GitHub App installation.

    Auth: Dashboard is behind Keycloak; this uses the App token, not user OAuth.
    """
    config = request.app.state.config
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    try:
        repos = await scm.list_installation_repos()
        return sorted(repos, key=lambda r: r["full_name"].lower())
    except Exception as e:
        logger.warning("Failed to list repos: %s", e)
        return []
    finally:
        await scm.close()


@router.get("/api/repos/check")
async def check_repo_name(request: Request, name: str, org: str = "tesserix") -> dict[str, Any]:
    """Check if a repository name already exists in the org."""
    if not name.strip():
        return {"available": False, "reason": "Name is required"}

    config = request.app.state.config
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    try:
        await scm.get_repo_info(f"{org}/{name.strip()}")
        return {"available": False, "reason": "Repository already exists"}
    except Exception:
        return {"available": True, "reason": ""}
    finally:
        await scm.close()


@router.post("/api/repos/create")
async def create_repo(request: Request) -> dict[str, Any]:
    """Create a new repository via the GitHub App."""
    body = await request.json()
    org = body.get("org", "tesserix")
    name = body.get("name", "").strip()
    description = body.get("description", "")
    private = body.get("private", True)

    if not name:
        raise HTTPException(status_code=400, detail="Repository name is required")

    # Check if repo already exists
    config = request.app.state.config
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    try:
        try:
            await scm.get_repo_info(f"{org}/{name}")
            raise HTTPException(status_code=409, detail=f"Repository '{org}/{name}' already exists")
        except HTTPException:
            raise
        except Exception:
            pass  # Repo doesn't exist — proceed to create

        repo = await scm.create_repo(org, name, description, private)
        return repo
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to create repo: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await scm.close()


@router.get("/api/projects")
async def list_projects(request: Request) -> list[dict[str, Any]]:
    """List GitHub Projects v2 for the configured org."""
    config = request.app.state.config
    org = getattr(config, "github_org", "tesserix")
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    try:
        projects = await scm.list_org_projects(org)
        return projects
    except Exception as e:
        logger.warning("Failed to list projects: %s", e)
        return []
    finally:
        await scm.close()


@router.post("/api/projects/create")
async def create_project_endpoint(request: Request) -> dict[str, Any]:
    """Create a GitHub Project v2 and optionally link it to a repo."""
    body = await request.json()
    title = body.get("title", "").strip()
    description = body.get("description", "")
    repo = body.get("repo", "")

    if not title:
        raise HTTPException(status_code=400, detail="Project title is required")

    config = request.app.state.config
    org = getattr(config, "github_org", "tesserix")
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    try:
        project = await scm.create_project(org, title, description)
        if repo:
            await scm.link_repo_to_project(project["id"], repo)
        return project
    except Exception as e:
        logger.warning("Failed to create project: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await scm.close()


@router.post("/api/repos/scaffold")
async def scaffold_repo(request: Request) -> dict[str, Any]:
    """Scaffold a repository with CI workflows, CLAUDE.md, and project structure."""
    body = await request.json()
    repo = body.get("repo", "").strip()
    project_title = body.get("project_title", "")
    tech_stack = body.get("tech_stack", "")

    if not repo:
        raise HTTPException(status_code=400, detail="Repository name is required")

    config = request.app.state.config
    from devai.scm.factory import create_scm_client

    scm = create_scm_client(config)
    created_files: list[str] = []
    try:
        default_branch = await scm.get_default_branch(repo)

        # Check existing files to avoid overwriting
        try:
            existing = await scm.list_files(repo, ref=default_branch)
            existing_names = {f.get("name", "") for f in existing}
        except Exception:
            existing_names = set()

        # 1. Create CLAUDE.md with project guardrails
        if "CLAUDE.md" not in existing_names:
            claude_md = _generate_claude_md(repo, tech_stack)
            await scm.create_or_update_file(
                repo,
                "CLAUDE.md",
                claude_md,
                "chore: add CLAUDE.md with project guardrails",
                default_branch,
            )
            created_files.append("CLAUDE.md")

        # 2. Create .github/workflows/ci.yml
        try:
            await scm.list_files(repo, ".github/workflows", ref=default_branch)
        except Exception:
            ci_workflow = _generate_ci_workflow(repo)
            await scm.create_or_update_file(
                repo,
                ".github/workflows/ci.yml",
                ci_workflow,
                "ci: add template CI workflow",
                default_branch,
            )
            created_files.append(".github/workflows/ci.yml")

            pr_template = _generate_pr_template()
            await scm.create_or_update_file(
                repo,
                ".github/pull_request_template.md",
                pr_template,
                "chore: add pull request template",
                default_branch,
            )
            created_files.append(".github/pull_request_template.md")

        # 3. Link to project if provided
        project_info = None
        if project_title:
            org = repo.split("/")[0]
            project_info = await scm.create_project(org, project_title)
            await scm.link_repo_to_project(project_info["id"], repo)

        return {
            "status": "scaffolded",
            "repo": repo,
            "files_created": created_files,
            "project": project_info,
        }
    except Exception as e:
        logger.warning("Failed to scaffold repo %s: %s", repo, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await scm.close()


def _generate_claude_md(repo: str, tech_stack: str = "") -> str:
    """Generate a CLAUDE.md with project-specific guardrails."""
    repo_name = repo.split("/")[-1]
    stack_section = ""
    if tech_stack:
        stack_section = f"""
## Tech Stack

{tech_stack}
"""

    return f"""# Claude Reference Guide — {repo_name}

## Project

- **Repository:** `{repo}`
- **GitHub Org:** `tesserix`
{stack_section}
## Critical Rules

### 1. Git Identity

Always configure before any commit:
```bash
git config user.name "sam123ben"
git config user.email "samyak.rout@gmail.com"
```

### 2. No AI References

NEVER include Claude, Copilot, Anthropic, AI tool, or Co-Authored-By mentions in:
- Commit messages, PR titles or descriptions
- Code comments or changelog entries

### 3. CI Build Procedure

The `tesserix` GitHub org has limited Actions minutes for private repos.
Every CI build requires a public-to-build-to-private cycle:
```bash
gh repo edit {repo} --visibility public --accept-visibility-change-consequences
git push origin main
# Wait for CI to complete
gh run list --repo {repo} --limit 3
# Once all green:
gh repo edit {repo} --visibility private --accept-visibility-change-consequences
```

### 4. No Manual kubectl apply

All K8s changes go through ArgoCD via the `tesserix-k8s` repo.

### 5. SQL Schemas

All database schemas live in `tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/`.
Application repos contain only ORM models — never raw .sql files.

## Development

```bash
# Install dependencies
# (add project-specific install commands here)

# Run locally
# (add project-specific run commands here)

# Lint
# (add project-specific lint commands here)

# Test
# (add project-specific test commands here)
```

## GCP & Infrastructure

- **GCP Project:** `tesseracthub-480811`
- **GCP Region:** `asia-south1`
- **GKE Cluster:** `tesseract-prod-in-gke`
"""


def _generate_ci_workflow(repo: str) -> str:
    """Generate a template CI workflow."""
    repo_name = repo.split("/")[-1]
    return f"""name: CI Build

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  packages: write
  id-token: write

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{{{ github.repository }}}}/{repo_name}
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: echo "Add linting steps here"

  test:
    name: Test
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
      - name: Test
        run: echo "Add test steps here"

  build:
    name: Build
    runs-on: ubuntu-latest
    needs: test
    if: github.event_name != 'pull_request'
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{{{ env.REGISTRY }}}}
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{{{ env.REGISTRY }}}}/${{{{ env.IMAGE_NAME }}}}:latest
"""


def _generate_pr_template() -> str:
    """Generate a pull request template."""
    return """## Summary

<!-- Brief description of changes -->

## Changes

-

## Test Plan

- [ ] Unit tests pass
- [ ] Manual testing completed

## Checklist

- [ ] Code follows project conventions
- [ ] No secrets or credentials committed
- [ ] CI passes
"""


@router.post("/api/pipeline/trigger")
async def trigger_pipeline(request: Request) -> dict[str, Any]:
    """Trigger a DevAI ALM pipeline run from the dashboard (LangGraph)."""
    import asyncio

    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    body = await request.json()
    repo = body["repo"]
    requirements = body.get("requirements", "")
    issue_number = body.get("issue_number")

    state = request.app.state.state_manager
    config = request.app.state.config

    trigger_type = "cli"
    trigger_ref = "dashboard"

    if issue_number:
        from devai.core.github_client import GitHubClient

        github = GitHubClient(config)
        issue = await github.get_issue(repo, issue_number)
        # Build full requirements from issue (same as webhook)
        labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        requirements = (
            f"# Requirement: Issue #{issue_number} — {issue.get('title', '')}\n"
            f"**Labels:** {', '.join(labels)}\n\n"
            f"## Description\n\n{issue.get('body', '')}"
        )
        trigger_type = "github_issue"
        trigger_ref = str(issue_number)
        await github.close()

    if not requirements:
        raise HTTPException(status_code=400, detail="Requirements text or issue number required")

    # Create a run ID immediately for the response
    from ulid import ULID

    run_id = str(ULID())

    # Run the LangGraph pipeline in the background
    async def _run_bg() -> None:
        from devai.core.github_client import GitHubClient
        from devai.graph.orchestrator import ALMOrchestrator

        github = GitHubClient(config)
        try:
            orchestrator = ALMOrchestrator(github, state, config)
            await orchestrator.run(
                repo_full_name=repo,
                requirements=requirements,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
            )
        except Exception:
            logger.exception("Background pipeline failed for %s", repo)
        finally:
            await github.close()

    asyncio.create_task(_run_bg())

    return {
        "run_id": run_id,
        "stage": "triggered",
        "repo": repo,
    }


@router.get("/api/pipeline/runs")
async def get_pipeline_runs(request: Request, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Get recent pipeline runs."""
    state = request.app.state.state_manager
    if repo:
        run_ids = await state.list_runs_by_repo(repo, limit)
    else:
        run_ids = await state.list_runs(limit)

    runs = []
    for rid in run_ids:
        run_data = await state.get_run(rid)
        if run_data:
            agents = await state.get_agent_statuses(rid)
            runs.append(
                {
                    "run_id": rid,
                    "stage": run_data.get("stage"),
                    "repo": run_data.get("repo"),
                    "created_at": run_data.get("created_at"),
                    "agents": agents,
                }
            )
    return runs


@router.get("/api/pipeline/runs/{run_id}")
async def get_pipeline_run(request: Request, run_id: str) -> dict[str, Any]:
    """Get detailed status of a pipeline run."""
    state = request.app.state.state_manager
    run_data = await state.get_run(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="Run not found")
    agents = await state.get_agent_statuses(run_id)
    run_data["agents"] = agents
    return run_data


# --- Governance (CLAUDE.md) ---


@router.get("/api/governance/claude-md")
async def get_claude_md(request: Request, repo: str) -> dict[str, str]:
    """Get the stored CLAUDE.md governance content for a repo."""
    redis = request.app.state.state_manager.redis
    content = await redis.get(f"devai:governance:{repo}:claude_md")
    return {"content": content or "", "repo": repo}


@router.post("/api/governance/claude-md")
async def save_claude_md(request: Request) -> dict[str, str]:
    """Save CLAUDE.md governance content for a repo."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()
    repo = body["repo"]
    content = body["content"]

    redis = request.app.state.state_manager.redis
    await redis.set(f"devai:governance:{repo}:claude_md", content)
    logger.info("CLAUDE.md governance updated for %s by %s", repo, session["user_login"])
    return {"status": "saved", "repo": repo}


# --- Approval Gates ---


@router.get("/api/pipeline/runs/{run_id}/approvals")
async def get_pending_approvals(request: Request, run_id: str) -> list[dict[str, Any]]:
    """Get pending approval gates for a pipeline run."""
    redis = request.app.state.state_manager.redis
    raw = await redis.lrange(f"devai:run:{run_id}:approvals", 0, -1)
    return [json.loads(item) for item in raw]


@router.post("/api/pipeline/runs/{run_id}/approvals/{gate}/approve")
async def approve_gate(request: Request, run_id: str, gate: str) -> dict[str, str]:
    """Approve a pending gate, allowing the pipeline to continue."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    redis = request.app.state.state_manager.redis
    # Signal the waiting agent to proceed
    await redis.set(f"devai:run:{run_id}:gate:{gate}", "approved", ex=3600)
    # Remove from pending list
    approvals = await redis.lrange(f"devai:run:{run_id}:approvals", 0, -1)
    for item in approvals:
        data = json.loads(item)
        if data.get("gate") == gate:
            await redis.lrem(f"devai:run:{run_id}:approvals", 1, item)
            break

    logger.info("Gate %s approved for run %s by %s", gate, run_id, session["user_login"])
    return {"status": "approved", "gate": gate}


@router.post("/api/pipeline/runs/{run_id}/approvals/{gate}/reject")
async def reject_gate(request: Request, run_id: str, gate: str) -> dict[str, str]:
    """Reject a pending gate, stopping the pipeline."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    redis = request.app.state.state_manager.redis
    await redis.set(f"devai:run:{run_id}:gate:{gate}", "rejected", ex=3600)
    approvals = await redis.lrange(f"devai:run:{run_id}:approvals", 0, -1)
    for item in approvals:
        data = json.loads(item)
        if data.get("gate") == gate:
            await redis.lrem(f"devai:run:{run_id}:approvals", 1, item)
            break

    logger.info("Gate %s rejected for run %s by %s", gate, run_id, session["user_login"])
    return {"status": "rejected", "gate": gate}


# --- Pipeline Permissions/Config ---


@router.post("/api/pipeline/config")
async def save_pipeline_config(request: Request) -> dict[str, str]:
    """Save pipeline configuration (permissions, model settings, etc.)."""
    session = await _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()
    repo = body.get("repo", "default")

    redis = request.app.state.state_manager.redis
    await redis.set(f"devai:config:{repo}", json.dumps(body))
    return {"status": "saved"}


@router.get("/api/pipeline/config")
async def get_pipeline_config(request: Request, repo: str = "default") -> dict[str, Any]:
    """Get pipeline configuration."""
    redis = request.app.state.state_manager.redis
    raw = await redis.get(f"devai:config:{repo}")
    if raw:
        return json.loads(raw)
    return {
        "auto_mode": False,
        "gates": {
            "deployment": True,
            "testing": True,
            "review": False,
            "merge": True,
            "createPR": False,
        },
        "claude_model": "claude-sonnet-4-20250514",
        "openai_model": "o3",
        "max_review_iterations": 3,
        "branch_template": "devai/{run_id}/{description}",
    }
