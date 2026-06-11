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

from devai.onboarding.routes import _valid_segment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scm", tags=["scm"])


async def _require_principal(request: Request):
    """Resolve the caller's Principal, gated by the ``require_auth`` flag.

    Behavior-neutral by default: when ``DEVAI_REQUIRE_AUTH`` is False (the
    current prod default) this returns the resolved principal or ``None`` for
    anonymous callers — exactly the legacy ``extract_principal`` path, so
    nothing changes until the flag is flipped in chart values (CHART-1). When
    ``require_auth`` is True it delegates to :func:`devai.authz.require_principal`,
    which 401s anonymous callers (no fallback to a literal ``operator``).

    This is the CODE-1 gate for the mutating SCM routes (create_repo /
    initialise / scan), which mutate GitHub state with the platform's
    privileged token.
    """
    config = getattr(request.app.state, "config", None)
    if config is not None and getattr(config, "require_auth", False):
        from devai.authz import require_principal

        return await require_principal(request)
    from devai.identity import extract_principal

    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        return None


def _check_segments(*segments: str) -> None:
    """Reject crafted owner/name/repo path segments before they reach the SCM
    client. Reuses onboarding's ``_valid_segment`` (alnum start, then
    alnum/dot/dash/underscore, bounded length, no ``..``) so a path-traversal
    or URL-encoded separator can never be interpolated into a GitHub API path.
    """
    for seg in segments:
        if not _valid_segment(seg):
            raise HTTPException(status_code=422, detail="invalid repo owner/name segment")


def _check_repo_full(repo: str) -> None:
    """Validate an ``owner/name`` repo reference passed as a query param."""
    parts = repo.split("/")
    if len(parts) != 2:
        raise HTTPException(status_code=422, detail="repo must be 'owner/name'")
    _check_segments(*parts)


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

    return getattr(settings, "scm_organization", "") or getattr(settings, "github_org", "") or "tesserix"


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


# Path inside a repo that proves DevAI has been initialised. We probe
# its existence with a HEAD-on-contents API call and treat 200 as
# 'initialised', 404 as 'not yet'. See /initialise below for what gets
# written.
PLATFORM_MARKER_PATH = ".platform/devai.yaml"


async def _probe_initialised(client, full_name: str, default_branch: str = "main") -> bool:
    """Return True iff ``.platform/devai.yaml`` exists on the default
    branch. Tolerates upstream errors (treat as 'unknown / not init')
    so a single slow repo doesn't break the whole listing."""
    try:
        await client.get_file_content(repo=full_name, path=PLATFORM_MARKER_PATH, ref=default_branch)
        return True
    except Exception:  # noqa: BLE001
        return False


@router.get("/repos")
async def list_repos(
    request: Request,
    q: str = Query("", description="Search filter (matches full_name + description)"),
    initialised: str = Query(
        "",
        description="Filter by DevAI init status: 'true' = only repos with .platform/devai.yaml; "
        "'false' = only repos without it; empty = all repos (no probe).",
    ),
) -> list[dict[str, Any]]:
    """List repos the configured PAT / installation can see.

    Order: most-recently-pushed first. Each entry exposes the fields
    the New Pipeline Run dialog needs (full_name, private, description,
    default_branch, html_url, pushed_at). ``q`` filters by substring
    over full_name + description (case-insensitive). Empty ``q``
    returns the first page (~100 entries).

    When ``initialised`` is set, each candidate is probed for the
    presence of ``.platform/devai.yaml`` on its default branch — the
    DevAI initialisation marker. Probes run in parallel and cap at 30
    so a misconfigured org doesn't fan out into thousands of API hits.
    """
    client = _client(request)
    try:
        repos = await client.list_installation_repos(per_page=100)
    except AttributeError:
        # Non-GitHub providers don't have installation_repos — fall back.
        raise HTTPException(
            status_code=501,
            detail="list_repos is only implemented for the GitHub provider today",
        ) from None
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
                "initialised": None,
            }
        )
    out.sort(key=lambda r: r.get("pushed_at", ""), reverse=True)

    init_filter = initialised.lower().strip()
    if init_filter in ("true", "false"):
        import asyncio

        # Cap fan-out: probe the top ~30 most-recent repos. Beyond that
        # users should narrow with ?q=.
        probe_set = out[:30]
        results = await asyncio.gather(
            *(_probe_initialised(client, r["full_name"], r["default_branch"]) for r in probe_set),
            return_exceptions=True,
        )
        for r, is_init in zip(probe_set, results, strict=False):
            r["initialised"] = bool(is_init) if not isinstance(is_init, Exception) else False
        want = init_filter == "true"
        out = [r for r in out if r["initialised"] is want]
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
    await _require_principal(request)
    _check_segments(body.name)
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
    pr_template = "## Summary\n\n## Test plan\n- [ ] ...\n\n## Risk\n\n_DevAI · auto-opened_\n"
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
    "queued": ("queued", "todo", "backlog"),
    "in_progress": ("in-progress", "wip", "doing"),
    "review": ("review", "in-review"),
    "deployed": ("deployed", "staging", "in-staging"),
    "shipped": ("shipped", "released", "production"),
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
    _check_repo_full(repo)
    client = _client(request)
    try:
        issues = await client.list_issues(repo=repo, state=state, limit=100)
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


# --------------------------------------------------------------------------- #
# Initialise — drop the .platform/ marker so this repo is "DevAI-tracked"
# --------------------------------------------------------------------------- #


_PLATFORM_FILES: dict[str, str] = {
    ".platform/devai.yaml": (
        "# DevAI platform marker\n"
        "#\n"
        "# This file's presence on the default branch tells DevAI that\n"
        "# the repo is enrolled in the agentic workflow. The Workflows\n"
        "# kanban (devai.tesserix.app/workflows) filters its repo picker\n"
        "# on this marker — repos without it are listed under ⌘K @ as\n"
        "# candidates to enroll.\n"
        "apiVersion: devai.tesserix.app/v1alpha1\n"
        "kind: PlatformConfig\n"
        "metadata:\n"
        "  name: devai\n"
        "spec:\n"
        "  # Blueprint to use as the default for new pipeline runs in this\n"
        "  # repo. Override per-task from the New Pipeline Run dialog.\n"
        "  defaultBlueprint: default\n"
        "  # Lane → labels (mirrors src/devai/scm/routes.py _LANE_LABELS).\n"
        "  # Override here to introduce repo-specific labels (e.g. a custom\n"
        "  # 'pending-design' label that should land in REVIEW).\n"
        "  lanes:\n"
        "    queued:      [queued, todo, backlog]\n"
        "    in_progress: [in-progress, wip, doing]\n"
        "    review:      [review, in-review]\n"
        "    deployed:    [deployed, staging, in-staging]\n"
        "    shipped:     [shipped, released, production]\n"
        "  # Specialisations that may dispatch in this repo. Empty list =\n"
        "  # all catalogued specialisations are permitted.\n"
        "  allowedSpecialisations: []\n"
    ),
    ".platform/README.md": (
        "# .platform — DevAI control surface\n\n"
        "DevAI uses this directory to track the repo. Don't delete it.\n\n"
        "## Files\n\n"
        "- `devai.yaml` — repo-level config (default blueprint, lane labels, "
        "specialisation allow-list).\n\n"
        "## Editing\n\n"
        "Edit `devai.yaml` and open a PR like any other change — DevAI re-reads "
        "it on the next pipeline run.\n"
    ),
}


class InitialiseResponse(BaseModel):
    full_name: str
    branch: str
    pr_url: str
    already_initialised: bool


@router.post("/repos/{owner}/{name}/initialise")
async def initialise_repo(request: Request, owner: str, name: str) -> InitialiseResponse:
    """Enroll a repo into the DevAI workflow surface.

    Steps:
      1. Check default branch.
      2. If ``.platform/devai.yaml`` already exists, return
         ``already_initialised=True`` (no-op, idempotent re-call).
      3. Otherwise:
         a. Branch off default as ``devai/init-platform``.
         b. Commit the two seed files (devai.yaml + README.md).
         c. Open a PR back to default. CI on the seed branch is
            skipped — the files are configuration, not code.
      4. Return PR url + branch name.

    The branch + PR approach (instead of direct-to-main) means
    operators see the enrolment in their normal review queue and can
    audit it. Auto-merge is intentionally NOT triggered here — the
    repo's existing branch-protection rules win.
    """
    await _require_principal(request)
    _check_segments(owner, name)
    client = _client(request)
    full = f"{owner}/{name}"

    try:
        default_branch = await client.get_default_branch(full)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"get_default_branch: {e}") from e

    if await _probe_initialised(client, full, default_branch):
        return InitialiseResponse(
            full_name=full,
            branch=default_branch,
            pr_url=f"https://github.com/{full}",
            already_initialised=True,
        )

    branch = "devai/init-platform"
    try:
        await client.create_branch(repo=full, branch_name=branch, from_branch=default_branch)
    except Exception as e:  # noqa: BLE001
        # GitHub returns 422 if the branch already exists from an
        # earlier failed run — that's fine, reuse it.
        if "422" not in str(e):
            raise HTTPException(status_code=502, detail=f"create_branch: {e}") from e

    for path, content in _PLATFORM_FILES.items():
        try:
            await client.create_or_update_file(
                repo=full,
                path=path,
                content=content,
                message=f"chore(devai): scaffold {path}",
                branch=branch,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"write {path}: {e}") from e

    try:
        pr = await client.create_pull_request(
            repo=full,
            title="chore(devai): initialise platform config",
            body=(
                "Enrolls this repo into the **DevAI** agentic workflow.\n\n"
                "Adds:\n\n"
                "- `.platform/devai.yaml` — repo-level config (default blueprint, lane labels)\n"
                "- `.platform/README.md` — what this directory is for\n\n"
                "After this merges, the Workflows kanban + Cmd-K repo pickers will list this repo."
            ),
            head_branch=branch,
            base_branch=default_branch,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"create_pull_request: {e}") from e

    return InitialiseResponse(
        full_name=full,
        branch=branch,
        pr_url=pr.get("html_url", f"https://github.com/{full}/pulls"),
        already_initialised=False,
    )


# --------------------------------------------------------------------------- #
# Scan — capture a repo profile into memory
# --------------------------------------------------------------------------- #


# Files the scan reads (top-level) — enough to detect the tech stack
# and write a short profile. We deliberately don't traverse — too many
# repos, too much I/O.
_SCAN_TARGETS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Dockerfile",
    "Makefile",
    "README.md",
    ".platform/devai.yaml",
)


def _classify_stack(found: dict[str, str]) -> dict[str, Any]:
    """Cheap, deterministic tech detection from the captured files."""
    languages: list[str] = []
    frameworks: list[str] = []
    package_managers: list[str] = []
    if "package.json" in found:
        languages.append("typescript" if "typescript" in found["package.json"].lower() else "javascript")
        package_managers.append("npm")
        pj = found["package.json"].lower()
        if '"next"' in pj:
            frameworks.append("next.js")
        if '"react"' in pj:
            frameworks.append("react")
        if '"vite"' in pj:
            frameworks.append("vite")
        if '"express"' in pj:
            frameworks.append("express")
    if "pyproject.toml" in found or "requirements.txt" in found:
        languages.append("python")
        package_managers.append("pip" if "requirements.txt" in found else "poetry/pip")
        body = (found.get("pyproject.toml", "") + found.get("requirements.txt", "")).lower()
        if "fastapi" in body:
            frameworks.append("fastapi")
        if "django" in body:
            frameworks.append("django")
        if "flask" in body:
            frameworks.append("flask")
        if "langchain" in body or "langgraph" in body:
            frameworks.append("langchain/langgraph")
    if "go.mod" in found:
        languages.append("go")
        package_managers.append("go modules")
        if "gin-gonic" in found["go.mod"]:
            frameworks.append("gin")
    if "Cargo.toml" in found:
        languages.append("rust")
        package_managers.append("cargo")
    if "Dockerfile" in found:
        frameworks.append("docker")
    return {
        "languages": sorted(set(languages)),
        "frameworks": sorted(set(frameworks)),
        "package_managers": sorted(set(package_managers)),
    }


class ScanResponse(BaseModel):
    full_name: str
    default_branch: str
    profile: dict[str, Any]
    files_seen: list[str]
    stored_in_memory: bool


@router.post("/repos/{owner}/{name}/scan")
async def scan_repo(request: Request, owner: str, name: str) -> ScanResponse:
    """Read the repo's top-level metadata, build a tech profile, store it.

    The captured profile lands in the memory adapter under
    ``repo_profiles/<owner>/<name>`` (semantic memory). Stages that
    dispatch into the repo can pull it back via the standard memory
    `recall(...)` API — that's the 'use the related existing info'
    contract the operator asked for.

    Fast path — we never recurse into the tree, only read a hard-
    coded set of top-level files. A full code-walk is the next step
    (`/scan/deep` later) but this profile is enough to seed agent
    prompts with 'what kind of repo am I in'.
    """
    await _require_principal(request)
    _check_segments(owner, name)
    client = _client(request)
    full = f"{owner}/{name}"

    try:
        default_branch = await client.get_default_branch(full)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"get_default_branch: {e}") from e

    found: dict[str, str] = {}
    for path in _SCAN_TARGETS:
        try:
            content = await client.get_file_content(repo=full, path=path, ref=default_branch)
        except Exception:  # noqa: BLE001
            continue
        # Cap at 16 KB per file — README/Dockerfile can be long; we
        # only need the first few KB for classification.
        found[path] = content[: 16 * 1024]

    stack = _classify_stack(found)
    readme_lead = ""
    if "README.md" in found:
        # First non-empty line that isn't a heading marker.
        for line in found["README.md"].splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                readme_lead = s[:240]
                break

    profile = {
        "full_name": full,
        "default_branch": default_branch,
        "tech": stack,
        "summary": readme_lead,
        "scanned_paths": list(found.keys()),
    }

    # Persist to the memory adapter so the pipeline can recall it on
    # future runs without re-scanning. Best-effort: a missing or noop
    # adapter still returns a useful response to the caller.
    stored = False
    try:
        memory = getattr(request.app.state, "memory_adapter", None)
        if memory is not None:
            # MemoryAdapter contract: positional content string + repo/tags
            # scoping. (The old category=/key= kwargs predate the adapter
            # family and crashed every scan-profile write.)
            import json as _json

            await memory.remember(
                f"Repo profile for {full}: {_json.dumps(profile, default=str)}",
                agent="repo_scanner",
                repo=full,
                memory_type="semantic",
                tags=["repo_profiles", "scan"],
                metadata={"profile": profile},
            )
            stored = True
    except Exception:  # noqa: BLE001
        logger.exception("memory.remember failed — profile not persisted")

    return ScanResponse(
        full_name=full,
        default_branch=default_branch,
        profile=profile,
        files_seen=list(found.keys()),
        stored_in_memory=stored,
    )
