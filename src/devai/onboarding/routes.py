"""FastAPI routes for the Repos onboarding surface.

Mounted under ``/api/scm/*`` (already proxied to devai-api by the
dashboard) so no next.config change is needed. Backs the Repos page:
org catalog + onboard + merge/assign-reviewer + reconcile.

Every route 503s cleanly when the onboarding service isn't wired (e.g.
SCM unconfigured) so the UI renders an empty state, not a hard error.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from devai.identity import extract_principal

if TYPE_CHECKING:
    from devai.onboarding.service import OnboardingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scm", tags=["onboarding"])

# A GitHub owner login or repo name: alnum start, then alnum / dot / dash /
# underscore. Bounded length. Rejecting anything else stops malformed or
# crafted segments (path traversal, URL-encoded separators, control chars)
# from ever reaching the `/repos/{owner}/{name}/...` GitHub API paths.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _valid_segment(value: str) -> bool:
    return bool(_SEGMENT_RE.match(value)) and ".." not in value


def _check_repo(owner: str, name: str) -> None:
    """Validate path-param owner/name before they hit the SCM client."""
    if not _valid_segment(owner) or not _valid_segment(name):
        raise HTTPException(status_code=422, detail="invalid repo owner/name")


def _service(request: Request) -> OnboardingService:
    svc = getattr(request.app.state, "onboarding_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="onboarding service unavailable (SCM not configured)")
    return svc


async def _principal(request: Request) -> str:
    """The authenticated actor for audit fields (who onboarded / merged).

    Resolved from the auth-bff ``X-Forwarded-*`` headers or the
    ``devai_session`` cookie via :func:`devai.identity.extract_principal`,
    so the value can't be spoofed through the request body. Falls back to
    ``'operator'`` only when no identity is resolvable (e.g. local dev).
    """
    try:
        p = await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup must never block the action
        p = None
    if p is not None:
        for attr in ("email", "display_name", "uid"):
            val = getattr(p, attr, None)
            if val:
                return str(val)
    return "operator"


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


@router.get("/org/repos")
async def list_org_repos(
    request: Request,
    q: str = Query("", description="Search filter (full_name + description)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    refresh: int = Query(0, description="1 = bypass the 5-min cache"),
    probe: int = Query(1, description="1 = probe marker presence for the page"),
) -> dict[str, Any]:
    """Paginated org catalog with onboarding status overlay.

    Lists every repo the GitHub App / token can see in the org, scoped to
    the current page, with each repo's onboarding ``state`` resolved from
    the store and a marker probe.
    """
    svc = _service(request)
    try:
        # Bound the whole catalog build so a slow upstream returns a clean JSON
        # error well before an edge gateway (e.g. Cloudflare ~100s) would cut
        # the connection and serve its own raw HTML 502 to the dashboard.
        return await asyncio.wait_for(
            svc.list_catalog(query=q, page=page, per_page=per_page, refresh=bool(refresh), probe=bool(probe)),
            timeout=75,
        )
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="catalog: upstream SCM timed out — try again or use Refresh") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"catalog: {e}") from e


# --------------------------------------------------------------------------- #
# Onboarded set + onboard action
# --------------------------------------------------------------------------- #


class RepoRef(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)

    @field_validator("owner", "name")
    @classmethod
    def _segment(cls, v: str) -> str:
        if not _valid_segment(v):
            raise ValueError("must be a valid GitHub owner/repo name (no path traversal)")
        return v


class OnboardRequest(BaseModel):
    # Cap the batch so one request can't open an unbounded number of PRs.
    repos: list[RepoRef] = Field(min_length=1, max_length=100)
    base_branch: str = Field("", max_length=255)
    description: str = Field("", max_length=1000)
    dry_run: bool = False


@router.get("/onboarded")
async def list_onboarded(
    request: Request, state: str = Query("", description="Filter by state")
) -> list[dict[str, Any]]:
    svc = _service(request)
    rows = await svc.list_onboarded(state=state or None)
    return [r.to_dict() for r in rows]


@router.post("/onboarded")
async def onboard_repos(request: Request, body: OnboardRequest) -> dict[str, Any]:
    """Onboard one or many repos — opens a gated PR adding the marker."""
    svc = _service(request)
    try:
        return await svc.onboard(
            repos=[(r.owner, r.name) for r in body.repos],
            onboarded_by=await _principal(request),
            base_branch=body.base_branch,
            description=body.description,
            dry_run=body.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"onboard: {e}") from e


class CreateRepoRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=1000)
    private: bool = True
    tech_stack: str = Field("", max_length=2000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _valid_segment(v):
            raise ValueError("must be a valid GitHub repo name (alnum, '.', '-', '_'; no path traversal)")
        return v


@router.post("/onboarded/create", status_code=201)
async def create_and_onboard_repo(request: Request, body: CreateRepoRequest) -> dict[str, Any]:
    """Create a brand-new repo from scratch, scaffold the default project
    files + quality gates (README, .github/workflows ci/pr/release, Dependabot,
    CODEOWNERS, .gitignore), commit the `.platform/devai.yaml` marker, and
    record it as ONBOARDED in one shot — no PR needed for a repo we just made."""
    import httpx

    svc = _service(request)
    try:
        return await svc.create_and_onboard(
            body.name,
            description=body.description,
            private=body.private,
            tech_stack=body.tech_stack,
            onboarded_by=await _principal(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        # The configured SCM token can read repos but isn't allowed to create
        # them — surface the real, actionable cause instead of a generic 502.
        if status in (401, 403):
            raise HTTPException(
                status_code=403,
                detail=(
                    "The configured GitHub token can't create repositories in this org. "
                    "A fine-grained PAT needs 'Administration: Read and write' repository "
                    "permission and access to all repositories (or a classic PAT with the "
                    "'repo' scope and org repo-creation rights). Update the SCM token and retry."
                ),
            ) from e
        if status == 422:
            # GitHub 422 on create usually means the name already exists.
            raise HTTPException(status_code=409, detail=f"repository '{body.name}' already exists") from e
        raise HTTPException(status_code=502, detail=f"create: {status} {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"create: {e}") from e


@router.get("/onboarded/{owner}/{name}")
async def get_onboarded(request: Request, owner: str, name: str) -> dict[str, Any]:
    _check_repo(owner, name)
    svc = _service(request)
    row = await svc.get(owner, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{owner}/{name} not onboarded")
    return row.to_dict()


class MergeRequest(BaseModel):
    method: str = Field(default="squash", description="merge | squash | rebase")

    @field_validator("method")
    @classmethod
    def _method(cls, v: str) -> str:
        if v not in ("merge", "squash", "rebase"):
            raise ValueError("method must be one of: merge, squash, rebase")
        return v


@router.post("/onboarded/{owner}/{name}/ready")
async def mark_onboarding_pr_ready(request: Request, owner: str, name: str) -> dict[str, Any]:
    """Promote the draft onboarding PR to a ready-for-review (real) PR."""
    _check_repo(owner, name)
    svc = _service(request)
    try:
        row = await svc.mark_ready(owner, name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"ready: {e}") from e
    return row.to_dict()


@router.post("/onboarded/{owner}/{name}/merge")
async def merge_onboarding_pr(
    request: Request, owner: str, name: str, body: MergeRequest | None = None
) -> dict[str, Any]:
    """Merge the onboarding PR from inside DevAI and flip the repo to onboarded."""
    _check_repo(owner, name)
    svc = _service(request)
    method = (body.method if body else "squash") or "squash"
    if method not in ("merge", "squash", "rebase"):
        raise HTTPException(status_code=422, detail="invalid merge method")
    try:
        row = await svc.merge(owner, name, method=method)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"merge: {e}") from e
    return row.to_dict()


class AssignRequest(BaseModel):
    reviewers: list[str] = Field(
        min_length=1, max_length=25, description="GitHub logins (or org/team) to request review from"
    )

    @field_validator("reviewers")
    @classmethod
    def _reviewers(cls, vs: list[str]) -> list[str]:
        # Login: alnum + hyphen. Team handle: org/team (each segment validated).
        login = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
        for r in vs:
            handle = r.strip().lstrip("@")
            parts = handle.split("/", 1) if "/" in handle else [handle]
            if not all(login.match(p) for p in parts):
                raise ValueError(f"invalid reviewer handle: {r!r}")
        return vs


@router.post("/onboarded/{owner}/{name}/assign")
async def assign_reviewer(request: Request, owner: str, name: str, body: AssignRequest) -> dict[str, Any]:
    """Request a reviewer on the onboarding PR (assign-to-approve flow)."""
    _check_repo(owner, name)
    svc = _service(request)
    try:
        return await svc.assign_reviewer(owner, name, body.reviewers)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"assign: {e}") from e


@router.delete("/onboarded/{owner}/{name}")
async def archive_onboarded(request: Request, owner: str, name: str) -> dict[str, Any]:
    _check_repo(owner, name)
    svc = _service(request)
    row = await svc.archive(owner, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{owner}/{name} not onboarded")
    return row.to_dict()


@router.post("/onboarded/reconcile")
async def reconcile(
    request: Request, org: str = Query("", description="Org to reconcile (default: configured org)")
) -> dict[str, Any]:
    """Rebuild / refresh the cache from the `.platform/devai.yaml` markers."""
    if org and not _valid_segment(org):
        raise HTTPException(status_code=422, detail="invalid org")
    svc = _service(request)
    try:
        return await svc.reconcile(org=org)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"reconcile: {e}") from e
