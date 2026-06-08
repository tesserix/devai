"""PreviewProfile resolver — turn any repo into a runnable full-stack spec.

This is the "one spec, one resolver" core from docs/LIVE_PREVIEW_PLAN.md. A
repo's file tree + a few key file contents resolve into a normalized
``PreviewProfile`` (frontend + optional backend + optional datastore), with
per-stack install/run commands, ports, hot-reload mode, and the API↔UI
wiring. The manifest builder consumes only this — so adding a framework is a
new detection rule + runtime-profile row, never new orchestration code.

Resolution tiers (highest precedence first):
  1. explicit  — docker-compose / package scripts / .platform devai.yaml
  2. detected  — signal rules over the file tree + package.json/pyproject/go.mod

Pure logic: no SCM/k8s here. The caller fetches the tree + key files and
passes them in, which keeps this unit fully unit-testable.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field


@dataclass(slots=True)
class ServiceSpec:
    name: str  # logical name within the profile (web / api / db)
    role: str  # frontend | backend | database
    workdir: str  # repo-relative dir ("" = root)
    image: str
    install: list[str]
    run: list[str]
    port: int
    env: dict[str, str] = field(default_factory=dict)
    hot_reload: str = "none"  # hmr | reload | restart | none
    depends_on: list[str] = field(default_factory=list)
    engine: str = ""  # database engine when role == database


@dataclass(slots=True)
class PreviewProfile:
    services: list[ServiceSpec]
    mode: str = "detected"  # explicit | detected
    notes: list[str] = field(default_factory=list)

    def frontend(self) -> ServiceSpec | None:
        return next((s for s in self.services if s.role == "frontend"), None)

    def backend(self) -> ServiceSpec | None:
        return next((s for s in self.services if s.role == "backend"), None)

    def database(self) -> ServiceSpec | None:
        return next((s for s in self.services if s.role == "database"), None)


# ── Runtime profiles (data, not code) ─────────────────────────────────

_NODE_IMAGE = "node:20-bookworm-slim"
_PY_IMAGE = "python:3.12-slim"
_GO_IMAGE = "golang:1.26-bookworm"


def _join(workdir: str, f: str) -> str:
    return posixpath.join(workdir, f) if workdir else f


def _pkg_mgr(files: set[str], workdir: str) -> tuple[str, str]:
    """(install_cmd, run_prefix) for a node workdir based on the lockfile.

    Checks the workdir lockfile AND the repo root — pnpm/yarn monorepos keep a
    single lockfile at the root, not per-package.
    """

    def has(f: str) -> bool:
        return _join(workdir, f) in files or f in files

    if has("pnpm-lock.yaml"):
        return "pnpm install --frozen-lockfile", "pnpm"
    if has("yarn.lock"):
        return "yarn install --frozen-lockfile", "yarn"
    return "npm install", "npm"


def _detect_frontend(files: set[str], contents: dict[str, str]) -> ServiceSpec | None:
    # Find the nearest package.json (root or a common monorepo FE subpath).
    candidates = ["", "apps/web", "apps/frontend", "web", "frontend", "client", "packages/web"]
    for wd in candidates:
        pj = posixpath.join(wd, "package.json") if wd else "package.json"
        if pj not in files:
            continue
        pkg = _parse_json(contents.get(pj, ""))
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {})
        install, _pm = _pkg_mgr(files, wd)
        # Framework → dev command (all bind 0.0.0.0:3000, HMR).
        if "next" in deps:
            run = "npx next dev -H 0.0.0.0 -p 3000"
        elif "vite" in deps or any("vite" in str(v) for v in scripts.values()):
            run = "npx vite --host 0.0.0.0 --port 3000"
        elif "react-scripts" in deps:
            run = "HOST=0.0.0.0 PORT=3000 npm start"
        elif "nuxt" in deps:
            run = "npx nuxt dev --host 0.0.0.0 --port 3000"
        elif "@sveltejs/kit" in deps or "svelte" in deps:
            run = "npx vite dev --host 0.0.0.0 --port 3000"
        elif "astro" in deps:
            run = "npx astro dev --host 0.0.0.0 --port 3000"
        elif "dev" in scripts:
            run = "npm run dev"
        else:
            continue
        return ServiceSpec(
            name="web",
            role="frontend",
            workdir=wd,
            image=_NODE_IMAGE,
            install=["sh", "-lc", install],
            run=["sh", "-lc", run],
            port=3000,
            hot_reload="hmr",
        )
    return None


def _detect_backend(files: set[str], contents: dict[str, str]) -> tuple[ServiceSpec | None, bool]:
    """Returns (backend, needs_db). Scans common BE subpaths."""
    candidates = ["", "apps/api", "apps/backend", "api", "backend", "server", "services/api"]
    for wd in candidates:

        def j(f: str, _wd: str = wd) -> str:
            return _join(_wd, f)

        # Python (FastAPI / Flask / Django)
        py_marker = next((j(m) for m in ("pyproject.toml", "requirements.txt") if j(m) in files), "")
        if py_marker:
            text = contents.get(py_marker, "").lower()
            needs_db = any(x in text for x in ("sqlalchemy", "asyncpg", "psycopg", "databases", "tortoise"))
            if "fastapi" in text or "uvicorn" in text:
                run = "uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
            elif "flask" in text:
                run = "flask --app app run --debug --host 0.0.0.0 --port 8000"
            elif "django" in text:
                run = "python manage.py runserver 0.0.0.0:8000"
            else:
                continue
            install = "pip install -e . 2>/dev/null || pip install -r requirements.txt 2>/dev/null || true"
            return (
                ServiceSpec(
                    name="api",
                    role="backend",
                    workdir=wd,
                    image=_PY_IMAGE,
                    install=["sh", "-lc", install],
                    run=["sh", "-lc", run],
                    port=8000,
                    hot_reload="reload",
                    depends_on=["db"] if needs_db else [],
                ),
                needs_db,
            )
        # Go (Gin / Echo / net/http)
        if j("go.mod") in files:
            text = contents.get(j("go.mod"), "").lower()
            needs_db = any(x in text for x in ("gorm", "pgx", "lib/pq", "database/sql"))
            run = "go run ./... 2>/dev/null || go run ."
            return (
                ServiceSpec(
                    name="api",
                    role="backend",
                    workdir=wd,
                    image=_GO_IMAGE,
                    install=["sh", "-lc", "go mod download"],
                    run=["sh", "-lc", run],
                    port=8080,
                    hot_reload="restart",
                    depends_on=["db"] if needs_db else [],
                ),
                needs_db,
            )
        # Node backend (Express / Nest / Fastify)
        pj = j("package.json")
        if pj in files:
            pkg = _parse_json(contents.get(pj, ""))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if any(x in deps for x in ("express", "@nestjs/core", "fastify", "koa")):
                needs_db = any(x in deps for x in ("pg", "prisma", "typeorm", "sequelize", "@prisma/client"))
                install, _pm = _pkg_mgr(files, wd)
                run = "npm run dev 2>/dev/null || npx tsx watch src/index.ts 2>/dev/null || node ."
                return (
                    ServiceSpec(
                        name="api",
                        role="backend",
                        workdir=wd,
                        image=_NODE_IMAGE,
                        install=["sh", "-lc", install],
                        run=["sh", "-lc", run],
                        port=8080,
                        hot_reload="restart",
                        depends_on=["db"] if needs_db else [],
                    ),
                    needs_db,
                )
    return (None, False)


def _database_service() -> ServiceSpec:
    return ServiceSpec(
        name="db",
        role="database",
        workdir="",
        image="postgres:16-alpine",
        install=[],
        run=[],
        port=5432,
        engine="postgres",
        env={"POSTGRES_USER": "preview", "POSTGRES_PASSWORD": "preview", "POSTGRES_DB": "app"},
    )


def resolve_profile(files: set[str], contents: dict[str, str]) -> PreviewProfile:
    """Resolve a repo's file tree + key file contents into a PreviewProfile.

    ``files`` is the set of repo-relative paths; ``contents`` maps a few key
    files (package.json, pyproject.toml, requirements.txt, go.mod, and any
    .platform/devai.yaml) to their text.
    """
    notes: list[str] = []
    services: list[ServiceSpec] = []

    fe = _detect_frontend(files, contents)
    if fe:
        services.append(fe)
        notes.append(f"frontend: {fe.workdir or 'root'} ({fe.run[-1]})")

    be, needs_db = _detect_backend(files, contents)
    if be:
        services.append(be)
        notes.append(f"backend: {be.workdir or 'root'} ({be.run[-1]})")
        if needs_db:
            services.append(_database_service())
            notes.append("database: ephemeral postgres (auto-seeded)")

    if not services:
        notes.append("no runnable frontend/backend detected — needs explicit .platform/devai.yaml preview config")

    profile = PreviewProfile(services=services, mode="detected", notes=notes)
    apply_wiring(profile)
    return profile


def apply_wiring(profile: PreviewProfile) -> None:
    """Auto API↔UI wiring: point the FE at the BE preview URL and the BE at
    the DB + the FE origin (CORS). URLs use ${...} placeholders the manifest
    builder fills with the per-session preview/api hosts."""
    fe, be, db = profile.frontend(), profile.backend(), profile.database()
    if fe and be:
        # The browser must reach the BE on its public host (SSR + CSR safe).
        api_url = "${API_PUBLIC_URL}"
        fe.env.setdefault("NEXT_PUBLIC_API_URL", api_url)
        fe.env.setdefault("VITE_API_URL", api_url)
        fe.env.setdefault("VITE_API_BASE", api_url)
        fe.env.setdefault("REACT_APP_API_URL", api_url)
        be.env.setdefault("CORS_ORIGINS", "${WEB_PUBLIC_URL}")
        be.env.setdefault("ALLOWED_ORIGINS", "${WEB_PUBLIC_URL}")
    if be and db:
        be.env.setdefault("DATABASE_URL", "postgresql://preview:preview@localhost:5432/app")


def _parse_json(text: str) -> dict:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


__all__ = ["ServiceSpec", "PreviewProfile", "resolve_profile", "apply_wiring"]
