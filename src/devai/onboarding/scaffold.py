"""Default project scaffold for repos created from scratch via DevAI.

When an operator creates a new repo through the Repos page, DevAI seeds it
with a sensible, language-agnostic baseline so it is immediately governed by
the same quality gates as every other onboarded repo:

  * ``README.md``                         — project front page
  * ``.gitignore`` / ``.editorconfig``    — hygiene + consistency
  * ``.github/pull_request_template.md``  — PR checklist
  * ``.github/CODEOWNERS``                — review-ownership gate (template)
  * ``.github/dependabot.yml``            — dependency update gate
  * ``.github/workflows/ci.yml``          — lint/test/build gate (stack-aware)
  * ``.github/workflows/pr.yml``          — Conventional-Commit PR-title gate
  * ``.github/workflows/release.yml``     — tag-driven GitHub Release
  * ``.platform/devai.yaml``              — the onboarding marker (added by the
                                            service via ``synthesize_marker``)

The CI workflow is written to be a no-op-green on an empty repo and to
activate real gates as soon as a stack manifest (package.json / pyproject.toml
/ go.mod) appears — so the scaffold never ships a red default branch.

Deliberately NO ``CLAUDE.md`` or any AI-tooling reference is written into the
target repo.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScaffoldFile:
    """One file to seed into a freshly created repo."""

    path: str
    content: str
    message: str  # commit message


def default_scaffold_files(
    repo: str,
    *,
    description: str = "",
    tech_stack: str = "",
) -> list[ScaffoldFile]:
    """Return the ordered default file set for a brand-new repo.

    ``repo`` is ``owner/name``. The ``.platform/devai.yaml`` marker is NOT
    included here — the onboarding service writes it with proper metadata via
    :func:`devai.onboarding.marker.synthesize_marker`.
    """
    name = repo.split("/")[-1]
    desc = description.strip() or f"{name} — managed by the DevAI platform."

    return [
        ScaffoldFile("README.md", _readme(name, repo, desc, tech_stack), "docs: add README"),
        ScaffoldFile(".gitignore", _GITIGNORE, "chore: add .gitignore"),
        ScaffoldFile(".editorconfig", _EDITORCONFIG, "chore: add .editorconfig"),
        ScaffoldFile(
            ".github/pull_request_template.md", _PR_TEMPLATE, "chore: add pull request template"
        ),
        ScaffoldFile(".github/CODEOWNERS", _codeowners(repo), "chore: add CODEOWNERS"),
        ScaffoldFile(".github/dependabot.yml", _DEPENDABOT, "chore: enable Dependabot updates"),
        ScaffoldFile(".github/workflows/ci.yml", _CI_WORKFLOW, "ci: add CI quality gates"),
        ScaffoldFile(".github/workflows/pr.yml", _PR_WORKFLOW, "ci: add PR validation gate"),
        ScaffoldFile(".github/workflows/release.yml", _RELEASE_WORKFLOW, "ci: add release workflow"),
    ]


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def _readme(name: str, repo: str, description: str, tech_stack: str) -> str:
    stack = f"\n## Tech stack\n\n{tech_stack}\n" if tech_stack.strip() else ""
    return f"""# {name}

{description}

> Onboarded to the **DevAI** platform. The presence of `.platform/devai.yaml`
> on the default branch enrols this repo; CI quality gates and PR/release
> automation live under `.github/`.
{stack}
## Getting started

```bash
git clone https://github.com/{repo}.git
cd {name}
```

## Development

This repo ships with default quality gates:

- **CI** (`.github/workflows/ci.yml`) — lint, test and build run automatically
  for the detected stack on every push and pull request.
- **PR validation** (`.github/workflows/pr.yml`) — pull request titles must
  follow [Conventional Commits](https://www.conventionalcommits.org/).
- **Releases** (`.github/workflows/release.yml`) — pushing a `v*` tag cuts a
  GitHub Release with auto-generated notes.
- **Dependabot** keeps GitHub Actions and dependencies up to date.

## Contributing

1. Branch from the default branch.
2. Open a pull request with a Conventional-Commit title (e.g. `feat: ...`).
3. Ensure CI is green and obtain a review before merging.
"""


def _codeowners(repo: str) -> str:
    org = repo.split("/")[0]
    return f"""# Code owners — every pull request requests review from these owners.
# Replace the placeholder below with the real GitHub team(s) or user(s),
# e.g. "*  @{org}/platform" or "*  @your-handle".
#
# Until edited, this acts as documentation only (an unknown team does not
# block merges, it just surfaces a warning in the PR's reviewers panel).

*  @{org}/maintainers
"""


_GITIGNORE = """# OS / editor
.DS_Store
Thumbs.db
*.swp
.idea/
.vscode/

# Environments / secrets
.env
.env.*
!.env.example
*.local

# Logs
*.log
logs/

# Node
node_modules/
dist/
build/
coverage/
.next/
out/

# Python
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.egg-info/

# Go
bin/
vendor/

# Build artifacts
*.tmp
"""


_EDITORCONFIG = """root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 2

[*.{py,go}]
indent_size = 4

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab
"""


_PR_TEMPLATE = """## Summary

<!-- What does this PR change and why? -->

## Changes

-

## Checklist

- [ ] PR title follows Conventional Commits (`feat:`, `fix:`, `chore:` …)
- [ ] CI is green (lint, tests, build)
- [ ] Tests added/updated for the change
- [ ] Docs updated where relevant
"""


_DEPENDABOT = """version: 2
updates:
  # Keep the CI workflows' actions current.
  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: chore

  # Ecosystems below only activate when their manifest exists in the repo;
  # Dependabot quietly ignores the ones that don't apply.
  - package-ecosystem: npm
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: chore

  - package-ecosystem: pip
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: chore

  - package-ecosystem: gomod
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: chore
"""


# CI is stack-aware via hashFiles(): each block only runs when its manifest is
# present, so a freshly scaffolded (empty) repo is green and the gates engage
# automatically as code lands.
_CI_WORKFLOW = """name: CI

on:
  push:
    branches: [main, master]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  quality-gates:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # --- Node / TypeScript ---
      - name: Setup Node
        if: hashFiles('package.json') != ''
        uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Node lint / test / build
        if: hashFiles('package.json') != ''
        run: |
          npm ci || npm install
          npm run lint --if-present
          npm test --if-present
          npm run build --if-present

      # --- Python ---
      - name: Setup Python
        if: hashFiles('pyproject.toml', 'requirements.txt') != ''
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Python lint / test
        if: hashFiles('pyproject.toml', 'requirements.txt') != ''
        run: |
          python -m pip install --upgrade pip
          pip install ruff pytest
          if [ -f pyproject.toml ]; then pip install -e ".[dev]" || pip install -e . || true; fi
          if [ -f requirements.txt ]; then pip install -r requirements.txt || true; fi
          ruff check .
          pytest -q

      # --- Go ---
      - name: Setup Go
        if: hashFiles('go.mod') != ''
        uses: actions/setup-go@v5
        with:
          go-version: "1.23"
      - name: Go build / vet / test
        if: hashFiles('go.mod') != ''
        run: |
          go build ./...
          go vet ./...
          go test ./...
"""


_PR_WORKFLOW = """name: PR

on:
  pull_request:
    types: [opened, edited, synchronize, reopened]

permissions:
  pull-requests: read

jobs:
  conventional-title:
    runs-on: ubuntu-latest
    steps:
      - name: Validate PR title (Conventional Commits)
        uses: amannn/action-semantic-pull-request@v5
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
"""


_RELEASE_WORKFLOW = """name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
"""
