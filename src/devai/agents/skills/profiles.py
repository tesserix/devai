"""SkillProfile registry — picks a stack-specific profile for any pipeline run.

Each profile carries everything an agent needs to act like a senior engineer
in that stack: directory layout, test framework, CI workflow, dev system
prompt, review checklist. Agents look up the profile from
``state["skill_profile_name"]`` and inject the matching sections into their
Claude system prompts.

Detection is two-phase:
  1. Static keyword matching against the requirements text + existing repo
     tech stack (cheap, deterministic, runs first).
  2. The Tech Detector confirms or overrides via the LLM if multiple
     candidates score similarly.

Profiles are intentionally small and focused — three good ones are worth
more than fifteen vague ones.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillProfile:
    """A senior-engineer-level playbook for one tech stack.

    All string fields are rendered into agent system prompts via
    ``render_for_developer()`` / ``render_for_qa()`` / etc. — keep them
    dense, concrete, and free of hedging language.
    """

    name: str
    """Stable identifier persisted to state, e.g. ``nextjs_react``."""

    display_name: str
    """Human-readable name for logs and dashboards."""

    keywords: tuple[str, ...]
    """Lowercase keywords that, if present in the requirements/repo, vote
    for this profile. Used by ``detect_skill_profile``."""

    languages: tuple[str, ...]
    """Languages this profile applies to (lowercase)."""

    package_manager: str
    """``npm``, ``pnpm``, ``go``, ``pip``, etc."""

    primary_framework: str
    """e.g. ``Next.js 15``, ``Gin``, ``FastAPI``."""

    test_framework: str
    """Unit test framework: ``vitest``, ``jest``, ``go test``, ``pytest``."""

    test_command: str
    """Shell command the agent runs to execute tests locally."""

    lint_command: str
    """Shell command the agent runs to lint."""

    format_command: str
    """Shell command the agent runs to format."""

    directory_layout: str
    """Multi-line description of the canonical directory structure for
    this stack. Rendered verbatim into the developer system prompt."""

    file_conventions: str
    """One-line conventions, e.g. ``Components: PascalCase.tsx``."""

    developer_guidance: str
    """Multi-line, role-specific instructions for the Senior Developer.
    Should explicitly cover: how to scaffold a new feature, what files
    to create, how to wire them together, what NOT to do."""

    qa_guidance: str
    """Multi-line instructions for the QA Tester. Covers test framework,
    common patterns, and required coverage targets."""

    review_checklist: str
    """Multi-line checklist the Staff Reviewer walks through."""

    db_guidance: str = ""
    """Optional: ORM/migration guidance for the DB Engineer."""

    ci_workflow_template: str = ""
    """GitHub Actions workflow YAML template the CI Builder writes when
    no workflow exists. Should include lint, test, build steps."""

    extra_dev_dependencies: tuple[str, ...] = field(default_factory=tuple)
    """Dev dependencies the developer should install when scaffolding."""

    def matches_score(self, text: str, repo_tech: dict[str, Any] | None = None) -> int:
        """Score how strongly the requirements text + repo tech stack
        match this profile. Higher = better match.
        """
        score = 0
        text_lower = (text or "").lower()
        for kw in self.keywords:
            if kw in text_lower:
                score += 2

        if repo_tech:
            lang = (repo_tech.get("language") or "").lower()
            framework = (repo_tech.get("framework") or "").lower()
            for needle in self.keywords:
                if needle in lang or needle in framework:
                    score += 5
        return score

    def render_for_developer(self) -> str:
        """Render the profile section to inject into the dev system prompt."""
        return f"""## Active Skill Profile: {self.display_name}

You are operating in **{self.display_name}** mode. Follow these conventions exactly:

### Tech
- Primary framework: {self.primary_framework}
- Languages: {", ".join(self.languages)}
- Package manager: {self.package_manager}
- Test framework: {self.test_framework}

### Directory Layout
{self.directory_layout}

### File Conventions
{self.file_conventions}

### Implementation Guidelines
{self.developer_guidance}

### Local Validation Commands
- Lint:   `{self.lint_command}`
- Format: `{self.format_command}`
- Tests:  `{self.test_command}`
"""

    def render_for_qa(self) -> str:
        """Render the QA-specific section."""
        return f"""## Active Skill Profile: {self.display_name}

### Test Framework
{self.test_framework} — run with `{self.test_command}`

### QA Guidance
{self.qa_guidance}
"""

    def render_for_reviewer(self) -> str:
        """Render the Staff Reviewer checklist."""
        return f"""## Active Skill Profile: {self.display_name}

### Review Checklist
{self.review_checklist}
"""

    def render_for_db_engineer(self) -> str:
        """Render the DB Engineer guidance, if any."""
        if not self.db_guidance:
            return ""
        return f"""## Active Skill Profile: {self.display_name}

### Database Guidance
{self.db_guidance}
"""


# ----------------------------------------------------------------------
# Profile registry
# ----------------------------------------------------------------------


NEXTJS_REACT = SkillProfile(
    name="nextjs_react",
    display_name="Next.js + React (TypeScript)",
    keywords=(
        "next.js", "nextjs", "next js", "react", "tsx", "jsx",
        "frontend", "ui", "dashboard", "web app", "single page app",
        "vercel",
    ),
    languages=("typescript", "javascript"),
    package_manager="npm",
    primary_framework="Next.js 15 (App Router)",
    test_framework="Vitest + React Testing Library",
    test_command="npm run test -- --run",
    lint_command="npm run lint",
    format_command="npx prettier --write .",
    directory_layout="""src/
├── app/                    # Next.js App Router
│   ├── layout.tsx          # Root layout (always wraps children)
│   ├── page.tsx            # Index route
│   ├── (auth)/             # Route groups for auth pages
│   ├── api/                # Route handlers (server-side)
│   └── globals.css         # Tailwind / global styles
├── components/             # Reusable client/server components
│   ├── ui/                 # Primitive UI components (Button, Card, etc.)
│   └── <feature>/          # Feature-grouped components
├── lib/                    # Pure functions, API clients, utilities
├── hooks/                  # Custom React hooks (use prefix mandatory)
└── types/                  # Shared TypeScript types

tests/
├── unit/                   # Vitest unit tests for components & hooks
└── e2e/                    # Playwright end-to-end tests (if applicable)

next.config.ts              # Next.js config (use .ts not .js)
tsconfig.json               # TypeScript strict mode ON
package.json
.eslintrc.json              # next/core-web-vitals + @typescript-eslint
vitest.config.ts            # jsdom env, setup file at ./vitest.setup.ts
""",
    file_conventions="""- Components: `PascalCase.tsx` (e.g. `UserCard.tsx`)
- Hooks: `useThing.ts` (must start with `use`)
- Utility modules: `kebab-case.ts`
- Tests co-located OR in `tests/unit/<component>.test.tsx`
- Server components by default; add `"use client"` only when needed
- Strict TypeScript — no `any`, no `// @ts-ignore`""",
    developer_guidance="""**Scaffolding a new UI feature** (the kind of work the user explicitly asked for):

1. Decide if it's a route or a component:
   - New page → create `src/app/<route>/page.tsx` + optional `loading.tsx` and `error.tsx`
   - New widget → create `src/components/<feature>/<Name>.tsx`

2. Default to **server components**. Add `"use client"` ONLY if you need:
   - Hooks (`useState`, `useEffect`, etc.)
   - Browser APIs (`window`, `localStorage`)
   - Event handlers

3. **Always create a unit test alongside the component** in `tests/unit/`.
   Use Vitest + React Testing Library:
   ```tsx
   import { render, screen } from "@testing-library/react";
   import { describe, expect, it } from "vitest";
   import { UserCard } from "@/components/users/UserCard";

   describe("UserCard", () => {
     it("renders the user name", () => {
       render(<UserCard name="Alice" />);
       expect(screen.getByText("Alice")).toBeInTheDocument();
     });
   });
   ```

4. **Tailwind for styling.** Don't write CSS modules unless the project
   already uses them. Group className strings with `clsx` for conditionals.

5. **State management.** Start with `useState`. Reach for Zustand or
   TanStack Query only when prop-drilling becomes painful or you need
   server-state caching.

6. **Data fetching.** Server components: `fetch()` directly. Client
   components: TanStack Query. Never use `useEffect` for fetching.

7. **Forms.** Use `react-hook-form` + `zod` for validation. Don't roll
   your own form state.

8. **Accessibility is a feature.** Every interactive element needs an
   accessible name (`aria-label` or visible text). Use semantic HTML
   (`<button>`, not `<div onClick>`).

**What to commit:**
- All `.tsx`, `.ts` source files
- `package.json` updates if you added dependencies
- The new test file
- NEVER commit `node_modules/`, `.next/`, or build artifacts.

**Before opening the PR**, run `npm run lint && npm run test -- --run`
locally via the validation tools and fix every error/warning. A failing
lint or test is a hard block.""",
    qa_guidance="""**Vitest + React Testing Library + Playwright**

For unit tests:
- One `.test.tsx` per component, co-located or in `tests/unit/`
- Test from the user's perspective — query by role/text, not by
  className or test ID unless absolutely necessary
- Cover: happy path, empty state, loading state, error state
- Mock fetch with `vi.fn()` and `vi.spyOn()` — never hit a real network
- Snapshot tests are NOT a substitute for assertion tests

For E2E (Playwright):
- One `.spec.ts` per user journey in `tests/e2e/`
- Use `page.getByRole()`, `page.getByLabel()`, `page.getByTestId()` —
  in that priority order
- Tests must be independent: each spec gets its own browser context
- No `setTimeout` or `page.waitForTimeout` — use Playwright's
  auto-waiting locators

Coverage target: every PR adds tests for the lines it touches. The QA
agent's job is to *write* the tests, run them, and report any
acceptance criteria that aren't covered.""",
    review_checklist="""- [ ] All new components have a co-located unit test
- [ ] No `any` or `// @ts-ignore`
- [ ] Server vs client component decision is intentional and minimal
- [ ] No `useEffect` for data fetching (use server components or
      TanStack Query)
- [ ] Accessibility: every interactive element has an accessible name
- [ ] No new dependencies without a clear reason in the PR description
- [ ] Tailwind classes used over inline styles or CSS modules
- [ ] No `node_modules/`, `.next/`, or `tsconfig.tsbuildinfo` committed
- [ ] Linter passes (`npm run lint`) with zero warnings
- [ ] Unit tests pass (`npm run test -- --run`) with zero failures""",
    ci_workflow_template="""name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "npm"
      - run: npm ci
      - run: npm run lint
      - run: npm run test -- --run
      - run: npm run build
""",
    extra_dev_dependencies=(
        "vitest",
        "@vitest/ui",
        "@testing-library/react",
        "@testing-library/jest-dom",
        "@testing-library/user-event",
        "jsdom",
        "@types/node",
    ),
)


REACT_VITE = SkillProfile(
    name="react_vite",
    display_name="React + Vite (TypeScript)",
    keywords=(
        "vite", "react spa", "single page", "create react app", "cra",
        "react router",
    ),
    languages=("typescript", "javascript"),
    package_manager="npm",
    primary_framework="React 19 + Vite 6",
    test_framework="Vitest + React Testing Library",
    test_command="npm run test -- --run",
    lint_command="npm run lint",
    format_command="npx prettier --write .",
    directory_layout="""src/
├── main.tsx                # Entry point — mounts <App />
├── App.tsx                 # Top-level component, router setup
├── routes/                 # If using react-router, one file per route
├── components/
│   ├── ui/                 # Primitive components
│   └── <feature>/
├── hooks/
├── lib/                    # API clients, helpers
├── types/
└── index.css

tests/
├── unit/
└── e2e/

vite.config.ts              # Vite config with @vitejs/plugin-react
tsconfig.json
package.json
""",
    file_conventions="""- Components: `PascalCase.tsx`
- Hooks: `useThing.ts`
- One default export per component file
- Strict TypeScript""",
    developer_guidance="""**Scaffolding a Vite + React feature:**

1. Choose: route (under `src/routes/`) or component (under
   `src/components/<feature>/`).
2. All components are client-side (Vite has no SSR by default), so no
   need for `"use client"` directives.
3. Routing: react-router v7. Define routes in `src/routes/` and wire
   them in `App.tsx`.
4. Data fetching: TanStack Query. Don't use raw `useEffect` + `fetch`.
5. Forms: react-hook-form + zod.
6. State: Zustand for global, `useState` for local.
7. Styling: Tailwind CSS unless the project already uses something else.
8. Always create a Vitest test next to the component.""",
    qa_guidance="""Same as Next.js profile — Vitest + React Testing Library for
unit tests, Playwright for E2E. Vite's `@vitejs/plugin-react` works
seamlessly with Vitest in jsdom mode.""",
    review_checklist="""- [ ] All new components have a unit test
- [ ] No `any` or `// @ts-ignore`
- [ ] Routes are typed (use `LoaderFunctionArgs` / typed route params)
- [ ] No `useEffect` for fetching — use TanStack Query
- [ ] Bundle size impact is reasonable (check the Vite build output)
- [ ] Linter clean, tests pass""",
    ci_workflow_template="""name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "npm"
      - run: npm ci
      - run: npm run lint
      - run: npm run test -- --run
      - run: npm run build
""",
)


GO_GIN = SkillProfile(
    name="go_gin",
    display_name="Go + Gin (PostgreSQL)",
    keywords=(
        "go", "golang", "gin", "echo", "fiber", "grpc", "go module",
        "go.mod",
    ),
    languages=("go",),
    package_manager="go modules",
    primary_framework="Gin 1.10",
    test_framework="go test (standard library + testify)",
    test_command="go test ./... -race -count=1",
    lint_command="golangci-lint run",
    format_command="gofmt -w . && goimports -w .",
    directory_layout="""cmd/
└── server/
    └── main.go             # Wires DI and starts the HTTP server

internal/                   # Private to this module
├── handlers/               # HTTP handlers, one file per resource
├── models/                 # GORM models / domain types
├── repositories/           # DB access layer
├── services/               # Business logic
├── middleware/             # Gin middleware
└── config/                 # Env loading via Viper or stdlib

migrations/                 # DO NOT — schemas live in tesserix-k8s
api/                        # OpenAPI specs (optional)

go.mod
go.sum
Makefile
.golangci.yml
Dockerfile
""",
    file_conventions="""- Files: `snake_case.go` (e.g. `user_handler.go`)
- Tests: `*_test.go` next to the file under test
- Public types/functions: `PascalCase`
- Package-private: `camelCase`
- One package per directory
- Errors are values: return `error`, never panic in production code""",
    developer_guidance="""**Scaffolding a Go service feature:**

1. **HTTP layer** — add a handler in `internal/handlers/<resource>_handler.go`.
   Bind the handler to a route in `cmd/server/main.go` (or wherever the
   router is constructed).

2. **Service layer** — business logic goes in `internal/services/`.
   Handlers should be thin: parse → call service → render.

3. **Repository layer** — DB access in `internal/repositories/`. Use
   GORM. Never write raw SQL in handlers or services.

4. **Tests** — every handler, service, and repository gets a `_test.go`
   file. Use table-driven tests:
   ```go
   func TestCreateUser(t *testing.T) {
       cases := []struct {
           name    string
           input   CreateUserInput
           wantErr bool
       }{
           {"valid", CreateUserInput{Name: "Alice"}, false},
           {"missing name", CreateUserInput{}, true},
       }
       for _, c := range cases {
           t.Run(c.name, func(t *testing.T) { ... })
       }
   }
   ```

5. **Errors** — wrap with `fmt.Errorf("doing X: %w", err)`. Never
   discard or log+swallow.

6. **Concurrency** — use `context.Context` for cancellation. Pass it
   through every function that does I/O.

7. **Logging** — structured logging via `slog`. No `fmt.Println` in
   production code.

8. **Dependencies** — add via `go get`, then `go mod tidy`. Never edit
   `go.mod` by hand for adds.

**Before PR:** `gofmt`, `goimports`, `golangci-lint run`, `go test ./...
-race`. All four must be clean.""",
    qa_guidance="""Go has a great built-in test framework. Use it.

- Unit tests: `*_test.go` next to the file. Table-driven, with
  descriptive `name` field.
- HTTP integration tests: use `httptest.NewServer` + a real `http.Client`,
  NOT mocked Gin contexts.
- Database tests: hit a real Postgres in a temp schema (`testcontainers-go`).
- Always run with `-race`.
- Coverage target: every PR adds tests for new code paths. `go test
  -coverprofile=coverage.out ./...` should not regress.""",
    db_guidance="""**GORM, not raw SQL.**

- Models in `internal/models/` with GORM tags.
- Migrations live in `tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/`,
  NOT in this repo. Local dev can use `db.AutoMigrate(&Model{})` for
  convenience but production schemas are managed externally.
- Foreign keys explicit. Use `Preload` for joins, not `Joins` for the
  common case.""",
    review_checklist="""- [ ] `gofmt` clean (no whitespace diffs)
- [ ] `golangci-lint run` clean
- [ ] All new exported functions have a doc comment
- [ ] Errors are wrapped with `%w`, not `%v`
- [ ] No `panic` in production code paths
- [ ] `context.Context` is the first arg of every I/O function
- [ ] Tests cover new code paths and run with `-race`
- [ ] No raw SQL in handlers or services
- [ ] No new external dependencies without justification
- [ ] go.mod and go.sum are tidy (`go mod tidy`)""",
    ci_workflow_template="""name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: "1.26"
          cache: true
      - run: go mod download
      - name: Lint
        uses: golangci/golangci-lint-action@v6
        with:
          version: latest
      - run: go test ./... -race -count=1
      - run: go build ./...
""",
)


PYTHON_FASTAPI = SkillProfile(
    name="python_fastapi",
    display_name="Python + FastAPI (SQLAlchemy)",
    keywords=(
        "python", "fastapi", "uvicorn", "starlette", "pydantic",
        "sqlalchemy", "alembic", "asyncio", "pytest",
    ),
    languages=("python",),
    package_manager="pip / hatch",
    primary_framework="FastAPI 0.115",
    test_framework="pytest + httpx",
    test_command="pytest -q",
    lint_command="ruff check src/",
    format_command="ruff format src/",
    directory_layout="""src/<package>/
├── __init__.py
├── main.py                 # FastAPI app factory
├── api/
│   ├── routes/             # One router per resource
│   └── deps.py             # Shared dependencies (auth, db session)
├── models/                 # SQLAlchemy models
├── schemas/                # Pydantic request/response schemas
├── services/               # Business logic
├── repositories/           # DB access
├── core/                   # config, security, logging
└── db.py                   # Engine + session factory

tests/
├── unit/
└── integration/

pyproject.toml
ruff.toml                   # or [tool.ruff] in pyproject.toml
Dockerfile
""",
    file_conventions="""- Modules: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Tests: `tests/unit/test_<module>.py`
- Type hints REQUIRED on every public function
- Async by default for I/O""",
    developer_guidance="""**Scaffolding a FastAPI feature:**

1. **Schemas first.** Define Pydantic request/response models in
   `src/<pkg>/schemas/<resource>.py`.

2. **Routes.** Add an `APIRouter` in `src/<pkg>/api/routes/<resource>.py`,
   include it in `main.py`. Routes are thin — they call services.

3. **Services.** Business logic in `src/<pkg>/services/<resource>.py`.
   Take a DB session via dependency injection, return domain types.

4. **Models.** SQLAlchemy 2.0 typed models in `src/<pkg>/models/`. Use
   `Mapped[T]` and `mapped_column()`.

5. **Async everywhere.** Use `AsyncSession`, `httpx.AsyncClient`, etc.
   Never mix sync ORM with async routes.

6. **Tests.** Co-located in `tests/`. Use `httpx.AsyncClient` against
   the FastAPI app for integration tests:
   ```python
   import pytest
   from httpx import AsyncClient

   @pytest.mark.asyncio
   async def test_create_user(client: AsyncClient):
       resp = await client.post("/users", json={"name": "Alice"})
       assert resp.status_code == 201
       assert resp.json()["name"] == "Alice"
   ```

7. **Type hints.** Every public function. Run `ruff check` before PR.

8. **Dependencies.** Add to `pyproject.toml`, pin if it's a production
   dep. Never to `requirements.txt` directly.

**Before PR:** `ruff check src/`, `ruff format src/`, `pytest -q`. All
must pass.""",
    qa_guidance="""**pytest with async support.**

- Unit tests: `tests/unit/test_<module>.py` mirroring source layout
- Integration: `tests/integration/test_<endpoint>.py` using
  `httpx.AsyncClient` against the real FastAPI app + a temp Postgres
- Use `pytest-asyncio` for async tests
- Fixtures in `tests/conftest.py` — db session, client, etc.
- Cover happy path + 4xx errors + 5xx error handling
- No mocking of the framework (httpx, FastAPI) — mock external services
  via `respx`""",
    db_guidance="""**SQLAlchemy 2.0 typed models, Alembic for migrations IN DEV ONLY.**

- Production schemas live in tesserix-k8s, not here.
- Use `Mapped[T]` and `mapped_column()` for type-safe models.
- Async sessions: `AsyncSession`, never mix with sync `Session`.
- Never use the old `Query` API — use `select()` statements.""",
    review_checklist="""- [ ] All public functions have type hints
- [ ] `ruff check` clean
- [ ] Routes are thin — services do the work
- [ ] No sync DB calls in async routes
- [ ] Tests cover the new paths (`pytest -q` clean)
- [ ] Pydantic schemas separate from SQLAlchemy models
- [ ] No `print()` — use `logging` or `structlog`
- [ ] Dependency added to `pyproject.toml`, not `requirements.txt`""",
    ci_workflow_template="""name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e ".[dev]"
      - run: ruff check src/
      - run: pytest -q
""",
)


# A safe fallback for unknown stacks — gives the agent enough structure
# to do something sensible without forcing the wrong conventions.
GENERIC = SkillProfile(
    name="generic",
    display_name="Generic (best-effort)",
    keywords=(),
    languages=(),
    package_manager="auto-detect",
    primary_framework="auto-detect",
    test_framework="auto-detect",
    test_command="auto-detect",
    lint_command="auto-detect",
    format_command="auto-detect",
    directory_layout="""Match the repository's existing layout. Use
``github_get_repo_tree`` to read the structure and follow the same
conventions.""",
    file_conventions="""Match the existing repository's conventions exactly.
Don't introduce new patterns.""",
    developer_guidance="""No specific skill profile was matched for this story.
Detect the stack from the existing repo files (use ``github_get_repo_tree``
and read ``package.json`` / ``go.mod`` / ``pyproject.toml`` / etc.) and
follow whatever the project already does. When in doubt, mirror the
nearest existing file.""",
    qa_guidance="""Detect the test framework from the existing repo and add
tests in the same style. If no tests exist, scaffold the simplest viable
setup for the detected language and add tests for the new code only.""",
    review_checklist="""- [ ] Code matches the repo's existing style
- [ ] No new dependencies without justification
- [ ] Tests cover the new code paths
- [ ] Linter and formatter clean""",
)


PROFILES: dict[str, SkillProfile] = {
    p.name: p
    for p in (NEXTJS_REACT, REACT_VITE, GO_GIN, PYTHON_FASTAPI, GENERIC)
}


def get_skill_profile(name: str | None) -> SkillProfile:
    """Look up a profile by name. Returns ``GENERIC`` if not found."""
    if not name:
        return GENERIC
    return PROFILES.get(name, GENERIC)


def detect_skill_profile(
    requirements_text: str,
    repo_tech: dict[str, Any] | None = None,
) -> SkillProfile:
    """Pick the best profile for a story.

    Scores every non-generic profile against the requirements text +
    repo tech stack. Returns the highest-scoring profile, or GENERIC if
    nothing scored above zero.
    """
    candidates: list[tuple[int, SkillProfile]] = []
    for profile in PROFILES.values():
        if profile.name == "generic":
            continue
        score = profile.matches_score(requirements_text, repo_tech)
        if score > 0:
            candidates.append((score, profile))

    if not candidates:
        logger.info("No skill profile matched, falling back to generic")
        return GENERIC

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_profile = candidates[0]
    logger.info(
        "Skill profile selected: %s (score %d) from %d candidates",
        best_profile.name, best_score, len(candidates),
    )
    return best_profile


# Tighten the keyword regex used for "ui" / "react" so we don't match
# arbitrary substrings inside other words.
_WORD_BOUNDARY = re.compile(r"\b\w+\b")
