"""Tech Stack Detector Agent — auto-detects language, framework, and runtime from repo.

Runs early in the pipeline to provide context for Engineering Manager and
Senior Developer. Detects:
- Primary language and version
- Web framework
- Database(s)
- Message queues
- Package manager
- Test framework
- CI/CD configuration
- Deployment target (K8s, VM, serverless)
- Existing patterns and conventions

Stores discoveries as semantic memory for future runs.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# File markers for tech stack detection
TECH_MARKERS: dict[str, dict[str, Any]] = {
    # Languages
    "go.mod": {"language": "go", "package_manager": "go modules"},
    "go.sum": {"language": "go"},
    "pyproject.toml": {"language": "python", "package_manager": "pip/hatch"},
    "requirements.txt": {"language": "python", "package_manager": "pip"},
    "Pipfile": {"language": "python", "package_manager": "pipenv"},
    "package.json": {"language": "javascript/typescript", "package_manager": "npm"},
    "pnpm-lock.yaml": {"package_manager": "pnpm"},
    "yarn.lock": {"package_manager": "yarn"},
    "bun.lockb": {"package_manager": "bun"},
    "Cargo.toml": {"language": "rust", "package_manager": "cargo"},
    "pom.xml": {"language": "java", "package_manager": "maven"},
    "build.gradle": {"language": "java/kotlin", "package_manager": "gradle"},
    # Frameworks
    "next.config.ts": {"framework": "next.js"},
    "next.config.js": {"framework": "next.js"},
    "next.config.mjs": {"framework": "next.js"},
    "vite.config.ts": {"framework": "vite"},
    "angular.json": {"framework": "angular"},
    "nuxt.config.ts": {"framework": "nuxt"},
    "svelte.config.js": {"framework": "svelte"},
    "manage.py": {"framework": "django"},
    "app.py": {"framework": "flask_or_fastapi"},
    # Infrastructure
    "Dockerfile": {"deployment": "container"},
    "docker-compose.yml": {"deployment": "docker-compose"},
    "docker-compose.yaml": {"deployment": "docker-compose"},
    "Makefile": {"build_system": "make"},
    "Jenkinsfile": {"ci": "jenkins"},
    ".github/workflows": {"ci": "github-actions"},
    ".gitlab-ci.yml": {"ci": "gitlab-ci"},
    "Chart.yaml": {"deployment": "helm/kubernetes"},
    "terraform": {"deployment": "terraform"},
    "serverless.yml": {"deployment": "serverless"},
    "fly.toml": {"deployment": "fly.io"},
    "render.yaml": {"deployment": "render"},
    "vercel.json": {"deployment": "vercel"},
    # Databases
    "prisma": {"database": "prisma"},
    "migrations": {"database": "sql_migrations"},
    "alembic": {"database": "alembic/sqlalchemy"},
    # Config
    ".eslintrc": {"linter": "eslint"},
    "ruff.toml": {"linter": "ruff"},
    ".golangci.yml": {"linter": "golangci-lint"},
    "jest.config": {"test_framework": "jest"},
    "vitest.config": {"test_framework": "vitest"},
    "playwright.config": {"test_framework": "playwright"},
    "pytest.ini": {"test_framework": "pytest"},
}

SYSTEM_PROMPT = """You are a tech stack detection expert. Given a repository file tree and key configuration files, identify:

1. Primary language and version
2. Web framework and version
3. Database(s) and ORM
4. Message queue / event system
5. Package manager
6. Test framework(s)
7. CI/CD platform
8. Deployment target
9. Key coding patterns/conventions
10. Existing API structure

Output ONLY valid JSON:
{
    "language": "go",
    "language_version": "1.26",
    "framework": "gin",
    "framework_version": "1.10",
    "databases": ["postgresql"],
    "orm": "gorm",
    "message_queue": "nats",
    "package_manager": "go modules",
    "test_framework": "go test",
    "ci_cd": "github-actions",
    "deployment": "kubernetes",
    "patterns": ["repository pattern", "middleware chain", "structured logging"],
    "api_style": "REST",
    "key_directories": {"handlers": "internal/handlers", "models": "internal/models"}
}"""


class TechDetectorAgent(BaseAgent):
    """Detects the tech stack of a repository for informed planning."""

    name = "tech_detector"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Detect the repository's technology stack."""
        repo = state.get("repo_full_name", "")

        if not repo:
            return {}

        # Get the repo file tree
        tree = await self.github.get_repo_tree(repo)
        file_paths = [item["path"] for item in tree if item.get("type") == "blob"]

        # Phase 1: Static detection from file markers
        detected: dict[str, Any] = {}
        for path in file_paths:
            filename = path.split("/")[-1]
            for marker, attrs in TECH_MARKERS.items():
                if filename == marker or path.endswith(marker) or marker in path:
                    detected.update(attrs)

        # Phase 2: Read key config files for version info
        config_contents: dict[str, str] = {}
        key_files = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml", "Dockerfile"]
        for f in key_files:
            matching = [p for p in file_paths if p.endswith(f)]
            if matching:
                try:
                    content = await self.github.get_file_content(repo, matching[0])
                    config_contents[f] = content[:3000]
                except Exception:
                    pass

        # Phase 3: Use Groq for intelligent analysis
        groq = GroqProvider(self.config)

        tree_summary = "\n".join(file_paths[:200])
        configs = "\n\n".join(f"=== {k} ===\n{v}" for k, v in config_contents.items())

        response = await groq.generate(
            prompt=f"""Analyze this repository:

## File Tree (first 200 files)
{tree_summary}

## Key Config Files
{configs}

## Pre-detected markers
{json.dumps(detected, indent=2)}

Identify the complete tech stack.""",
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            tech_stack = json.loads(response)
        except json.JSONDecodeError:
            tech_stack = detected

        # Merge static detection with LLM analysis
        for key, val in detected.items():
            if key not in tech_stack:
                tech_stack[key] = val

        # Store as semantic memory for future runs
        try:
            from devai.services.memory import AgentMemory

            memory = AgentMemory(self.state.redis)
            await memory.learn_repo_pattern(
                repo=repo,
                pattern_type="tech_stack",
                description=f"Tech stack: {json.dumps(tech_stack)}",
            )
        except Exception:
            pass

        # Notify downstream agents
        lang = tech_stack.get("language", "unknown")
        framework = tech_stack.get("framework", "unknown")
        deployment = tech_stack.get("deployment", "unknown")

        a2a.notify(
            "engineering_manager",
            "Tech Stack Detected",
            f"{repo}: {lang} + {framework}, deploy={deployment}",
            payload=tech_stack,
        )

        a2a.notify(
            "senior_developer",
            "Tech Stack Context",
            f"Repo uses {lang}/{framework}. Follow existing patterns: " + ", ".join(tech_stack.get("patterns", [])[:5]),
            payload=tech_stack,
        )

        a2a.notify(
            "security_expert",
            "Tech Stack for Security Scan",
            f"Primary language: {lang}. Scan accordingly.",
            payload={"language": lang, "framework": framework},
        )

        return {
            # Store tech stack in state for all agents to use
        }
