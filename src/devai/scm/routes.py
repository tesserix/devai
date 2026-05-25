"""FastAPI routes that expose the SCM client to the dashboard.

Mounted at ``/api/scm/*`` by webhook/app.py. Backs the New Pipeline Run
dialog (repository picker + create-new + project board picker) and the
Workflows kanban (per-repo issue feed grouped by lane).

All routes 503 cleanly when no SCM client is configured so the UI can
render an empty state rather than a hard error.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scm", tags=["scm"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _client(request: Request):
    """Resolve the SCM client. Built lazily from settings on first call.

    devai-api today instantiates the SCM client inside the pipeline /
    chat code paths. The dashboard never used it from the webhook, so
    we lazily build + cache on app.state — keeps cold start lean.
    """
    client = getattr(request.app.state, "scm_client", None)
    if client is not None:
        return client

    try:
        from devai.config import settings
        from devai.scm.factory import create_scm_client
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"SCM module not importable: {e}") from e

    try:
        client = create_scm_client(settings)
    except Exception as e:  # noqa: BLE001
        logger.exception("SCM client build failed")
        raise HTTPException(status_code=503, detail=f"SCM client unavailable: {e}") from e

    request.app.state.scm_client = client
    return client


def _org(request: Request) -> str:
    """Default org for create-repo + list-repos lookups. Resolves from
    settings.scm_organization (if set) or settings.github_org."""
    from devai.config import settings

    return (
        getattr(settings, "scm_organization", "")
        or getattr(settings, "github_org", "")
        or "tesserix"
    )


# --------------------------------------------------------------------------- #
# Repos
# --------------------------------------------------------------------------- #


class CreateRepoRequest(BaseModel):
    name: str = Field(min_length=1, description="Repo name (kebab-case)")
    description: str = Field(default="", description="One-liner")
    private: bool = Field(default=True)
    initialize: bool = Field(
        default=True,
        description="Seed .github/workflows, CLAUDE.md guardrails, PR template",
    )


@router.get("/repos")
async def list_repos(request: Request, q: str = Query("", description="Search filter")) -> list[dict[str, Any]]:
    """List repos the configured PAT / installation can see.

    Order: most-recently-pushed first. Each entry exposes the fields
    the New Pipeline Run dialog needs (full_name, private, description,
    default_branch, html_url, pushed_at). ``q`` filters by substring
    over full_name + description (case-insensitive). Empty ``q``
    returns the first page (~100 entries).
    """
    client = _client(request)
    try:
        repos = await client.list_installation_repos(per_page=100)
    except AttributeError:
        # Non-GitHub providers don't have installation_repos — fall back.
        raise HTTPException(
            status_code=501,
            detail="list_repos is only implemented for the GitHub provider today",
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"upstream: {e}") from e

    out: list[dict[str, Any]] = []
    q_lower = q.lower().strip()
    for r in repos:
        full = r.get("full_name", "")
        desc = r.get("description") or ""
        if q_lower and q_lower not in full.lower() and q_lower not in desc.lower():
            continue
        out.append(
            {
                "full_name": full,
                "name": r.get("name", ""),
                "owner": (r.get("owner") or {}).get("login", ""),
                "description": desc,
                "private": bool(r.get("private")),
                "default_branch": r.get("default_branch", "main"),
                "html_url": r.get("html_url", ""),
                "pushed_at": r.get("pushed_at", ""),
                "language": r.get("language") or "",
            }
        )
    out.sort(key=lambda r: r.get("pushed_at", ""), reverse=True)
    return out


@router.post("/repos")
async def create_repo(request: Request, body: CreateRepoRequest) -> dict[str, Any]:
    """Create a fresh repo + (optionally) seed it.

    Seeding writes three starter files:

        .github/workflows/ci.yaml — minimal CI placeholder
        CLAUDE.md                  — guardrails the agent reads before any commit
        .github/pull_request_template.md
                                   — review template

    The new repo lands fully private under the configured org so the
    pipeline can immediately open issues / PRs against it.
    """
    client = _client(request)
    org = _org(request)
    try:
        repo = await client.create_repo(
            org=org,
            name=body.name,
            description=body.description,
            private=body.private,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"create_repo: {e}") from e

    full = repo.get("full_name") or f"{org}/{body.name}"
    if body.initialize:
        try:
            await _seed_repo(client, full)
        except Exception:  # noqa: BLE001
            logger.exception("repo created but seeding failed — leaving empty")

    return {
        "full_name": full,
        "html_url": repo.get("html_url", ""),
        "default_branch": repo.get("default_branch", "main"),
        "private": bool(repo.get("private", True)),
        "initialized": body.initialize,
    }


async def _seed_repo(client, full: str) -> None:
    """Drop the standard DevAI scaffolding into a brand-new repo.

    Idempotent on re-call — create_or_update_file overwrites cleanly."""
    workflow = (
        "name: CI\n"
        "on:\n"
        "  push: { branches: [main] }\n"
        "  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v5\n"
        "      - run: echo 'placeholder — DevAI will replace this on the first run'\n"
    )
    claude_md = (
        "# Claude Guardrails\n\n"
        "This repo is managed by **DevAI**. The autonomous agents read this file\n"
        "before every commit / PR.\n\n"
        "## Hard rules\n"
        "- Never force-push.\n"
        "- Never disable tests.\n"
        "- Never commit secrets — read from GCP Secret Manager via ESO.\n"
        "- Match existing conventions; don't reformat unrelated code.\n\n"
        "## Conventions\n"
        "- Conventional commits (feat / fix / chore / refactor / docs / test).\n"
        "- One feature → one PR; rebase before merge.\n"
    )
    pr_template = (
        "## Summary\n\n"
        "## Test plan\n- [ ] ...\n\n"
        "## Risk\n\n"
        "_DevAI · auto-opened_\n"
    )
    for path, content in (
        (".github/workflows/ci.yaml", workflow),
        ("CLAUDE.md", claude_md),
        (".github/pull_request_template.md", pr_template),
    ):
        await client.create_or_update_file(
            repo=full,
            path=path,
            content=content,
            message=f"chore: scaffold {path}",
            branch="main",
        )


# --------------------------------------------------------------------------- #
# Issues — backs the Workflows kanban
# --------------------------------------------------------------------------- #


# Lane → labels. Each lane shows issues whose labels match. Multi-label
# issues land in the first lane that matches (priority left-to-right).
_LANE_LABELS: dict[str, tuple[str, ...]] = {
    "queued":      ("queued", "todo", "backlog"),
    "in_progress": ("in-progress", "wip", "doing"),
    "review":      ("review", "in-review"),
    "deployed":    ("deployed", "staging", "in-staging"),
    "shipped":     ("shipped", "released", "production"),
}


def _classify_issue(labels: list[str], state: str) -> str:
    """Assign one of the five lanes from the issue's labels.

    Falls back to ``queued`` if no label matched (so freshly-opened
    issues still appear). Closed issues default to ``shipped``."""
    label_set = {lbl.lower() for lbl in labels}
    for lane, candidates in _LANE_LABELS.items():
        if label_set.intersection(candidates):
            return lane
    if state == "closed":
        return "shipped"
    return "queued"


@router.get("/issues")
async def list_issues(
    request: Request,
    repo: str = Query(..., description="owner/name"),
    state: str = Query("all", description="open | closed | all"),
) -> dict[str, list[dict[str, Any]]]:
    """Issue feed grouped by lane.

    Returns ``{lane_key: [issue, ...]}`` — exactly the shape the
    /workflows kanban renders. Each card carries number / title /
    labels / state / updated_at / html_url and the assigned lane.
    """
    client = _client(request)
    try:
        issues = await client.list_issues(repo=repo, state=state, per_page=100)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"list_issues: {e}") from e

    lanes: dict[str, list[dict[str, Any]]] = {
        "queued": [],
        "in_progress": [],
        "review": [],
        "deployed": [],
        "shipped": [],
    }
    for i in issues:
        # PRs come back from /issues too — filter those out so the
        # kanban only shows real issues.
        if "pull_request" in i:
            continue
        labels = [(lbl.get("name") or "") for lbl in (i.get("labels") or [])]
        lane = _classify_issue(labels, i.get("state", "open"))
        lanes[lane].append(
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "state": i.get("state", "open"),
                "labels": labels,
                "updated_at": i.get("updated_at", ""),
                "html_url": i.get("html_url", ""),
                "lane": lane,
                "assignee": ((i.get("assignee") or {}).get("login") or ""),
            }
        )
    # Most recently touched first within each lane.
    for arr in lanes.values():
        arr.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return lanes
