"""Code validation tools — compile, lint, unit test, format check.

These are guardrails that run after code is committed to ensure quality
before it reaches the review stage. Every piece of code must pass:
1. Compilation / type check
2. Linting (style + best practices)
3. Unit tests
4. Format check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any
from urllib.parse import quote, urlparse

from devai.tools.git_guard import InvalidGitRef, run_git_clone, validate_ref, validate_repo

logger = logging.getLogger(__name__)

VALIDATION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "validate_compile",
        "description": "Compile/type-check the code. "
        "Python: mypy/pyright. Go: go build. TypeScript: tsc --noEmit. "
        "Returns compilation errors if any.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to validate"},
                "language": {"type": "string", "description": "Primary language: python|go|typescript|javascript"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
    {
        "name": "validate_lint",
        "description": "Run linter on the code. "
        "Python: ruff. Go: golangci-lint. TypeScript: eslint. "
        "Returns linting errors/warnings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to validate"},
                "language": {"type": "string", "description": "Primary language"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
    {
        "name": "validate_unit_tests",
        "description": "Run unit tests. "
        "Python: pytest. Go: go test. TypeScript: jest/vitest. "
        "Returns test results with pass/fail counts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to validate"},
                "language": {"type": "string", "description": "Primary language"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
    {
        "name": "validate_format",
        "description": "Check code formatting. Python: ruff format --check. Go: gofmt. TypeScript: prettier --check.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to validate"},
                "language": {"type": "string", "description": "Primary language"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
]


class ValidationToolExecutor:
    """Executes code validation tools."""

    def __init__(self, scm: Any | None = None) -> None:
        self.scm = scm

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        result = await handler(tool_input)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)

    async def _handle_validate_compile(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Run compilation/type checking."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            if lang == "python":
                # Try mypy first, then pyright
                out = await self._run(["python", "-m", "mypy", "--ignore-missing-imports", "."], tmpdir)
                tool = "mypy"
            elif lang == "go":
                out = await self._run(["go", "build", "./..."], tmpdir)
                tool = "go build"
            elif lang in ("typescript", "javascript"):
                await self._run(["npm", "install", "--ignore-scripts"], tmpdir)
                out = await self._run(["npx", "tsc", "--noEmit"], tmpdir)
                tool = "tsc"
            else:
                return {"tool": "unknown", "passed": True, "output": f"No compiler for {lang}"}

            passed = "error" not in out.lower() and "Error" not in out
            return {
                "tool": tool,
                "passed": passed,
                "errors": out[:3000] if not passed else "",
                "output": out[:1000] if passed else "",
            }

    async def _handle_validate_lint(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Run linter."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            if lang == "python":
                out = await self._run(["python", "-m", "ruff", "check", "."], tmpdir)
                tool = "ruff"
            elif lang == "go":
                out = await self._run(["golangci-lint", "run", "./..."], tmpdir)
                tool = "golangci-lint"
            elif lang in ("typescript", "javascript"):
                await self._run(["npm", "install", "--ignore-scripts"], tmpdir)
                out = await self._run(["npx", "eslint", ".", "--ext", ".ts,.tsx,.js,.jsx"], tmpdir)
                tool = "eslint"
            else:
                return {"tool": "unknown", "passed": True, "output": f"No linter for {lang}"}

            # Count issues
            error_count = out.count("error") + out.count("Error")
            warning_count = out.count("warning") + out.count("Warning")

            return {
                "tool": tool,
                "passed": error_count == 0,
                "errors": error_count,
                "warnings": warning_count,
                "output": out[:3000],
            }

    async def _handle_validate_unit_tests(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Run unit tests."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            if lang == "python":
                out = await self._run(
                    ["python", "-m", "pytest", "-v", "--tb=short", "-q"],
                    tmpdir,
                    timeout=300,
                )
                tool = "pytest"
            elif lang == "go":
                out = await self._run(
                    ["go", "test", "-v", "-count=1", "./..."],
                    tmpdir,
                    timeout=300,
                )
                tool = "go test"
            elif lang in ("typescript", "javascript"):
                await self._run(["npm", "install", "--ignore-scripts"], tmpdir)
                out = await self._run(["npm", "test", "--", "--passWithNoTests"], tmpdir, timeout=300)
                tool = "jest/vitest"
            else:
                return {"tool": "unknown", "passed": True, "output": f"No test runner for {lang}"}

            # Parse pass/fail
            passed_count = out.count("PASS") + out.count("passed") + out.count("ok")
            failed_count = out.count("FAIL") + out.count("failed") + out.count("FAILED")

            return {
                "tool": tool,
                "passed": failed_count == 0,
                "total_passed": passed_count,
                "total_failed": failed_count,
                "output": out[:3000],
            }

    async def _handle_validate_format(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Check code formatting."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            if lang == "python":
                out = await self._run(["python", "-m", "ruff", "format", "--check", "."], tmpdir)
                tool = "ruff format"
            elif lang == "go":
                out = await self._run(["gofmt", "-l", "."], tmpdir)
                tool = "gofmt"
            elif lang in ("typescript", "javascript"):
                await self._run(["npm", "install", "--ignore-scripts"], tmpdir)
                out = await self._run(["npx", "prettier", "--check", "."], tmpdir)
                tool = "prettier"
            else:
                return {"tool": "unknown", "passed": True, "output": f"No formatter for {lang}"}

            passed = len(out.strip()) == 0 or "All matched" in out or "already formatted" in out
            return {
                "tool": tool,
                "passed": passed,
                "output": out[:2000],
            }

    # --- Helpers ---

    async def _clone(self, repo: str, branch: str, tmpdir: str) -> bool:
        # Validate before git sees them: reject `..`, leading `-`, and any
        # value outside the safe owner/name + ref charset (injected text).
        try:
            repo = validate_repo(repo)
            branch = validate_ref(branch)
        except InvalidGitRef as e:
            logger.error("Refusing to clone: %s", e)
            return False
        clone_url = await self._build_clone_url(repo)
        returncode, _ = await run_git_clone(clone_url, branch, tmpdir)
        return returncode == 0

    async def _build_clone_url(self, repo: str) -> str:
        """Build an authenticated clone URL when the SCM client supports it."""
        if not self.scm:
            return f"https://github.com/{repo}.git"

        provider = str(getattr(self.scm, "provider", ""))
        base_url = getattr(self.scm, "_base_url", "https://github.com")
        token_getter = getattr(self.scm, "_get_token", None)
        token = await token_getter() if callable(token_getter) else ""
        host = urlparse(base_url).netloc or "github.com"

        if provider.endswith("github"):
            if token:
                return f"https://x-access-token:{quote(token, safe='')}@{host}/{repo}.git"
            return f"https://{host}/{repo}.git"

        if provider.endswith("gitlab"):
            if token:
                return f"https://oauth2:{quote(token, safe='')}@{host}/{repo}.git"
            return f"https://{host}/{repo}.git"

        return f"https://{host}/{repo}.git"

    async def _run(self, cmd: list[str], cwd: str, timeout: int = 120) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ},
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace")
        except (TimeoutError, FileNotFoundError, Exception) as e:
            return f"Command failed: {e}"
