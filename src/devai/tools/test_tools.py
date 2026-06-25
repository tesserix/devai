"""Playwright test execution tools for the QA Tester agent."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from devai.services.redact import redact_secrets
from devai.tools.git_guard import InvalidGitRef, run_git_clone, validate_ref, validate_repo

logger = logging.getLogger(__name__)

TEST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_playwright_test",
        "description": (
            "Run the repo's tests and return real results. Executes Playwright locally when a "
            "node toolchain is available; otherwise runs via the repo's OWN CI workflow "
            "(GitHub Actions) on the given branch — commit and push your test files first "
            "(github_commit_file), then call this to wait for and read the CI verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch with test files"},
                "test_file": {
                    "type": "string",
                    "description": "Specific test file to run (optional, runs all if omitted)",
                },
                "base_url": {"type": "string", "description": "Base URL for the application under test"},
            },
            "required": ["repo", "branch", "base_url"],
        },
    },
    {
        "name": "parse_test_results",
        "description": "Parse Playwright JSON test results into a structured summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "results_json": {"type": "string", "description": "Raw JSON test results from Playwright"},
            },
            "required": ["results_json"],
        },
    },
]


class TestToolExecutor:
    """Executes Playwright test tools."""

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

    async def _handle_run_playwright_test(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Run the repo's tests — locally when a node toolchain exists,
        otherwise through the repo's OWN CI (GitHub Actions).

        The production api pod is a Python image with git but NO node/npm,
        so the local path can never execute there — the QA agent used to get
        a FileNotFoundError it misread as "repository access issues". The CI
        path is the honest execution route: the agent has already pushed its
        commits (which triggered the workflow), so we locate the branch's
        workflow run, wait for it, and return the REAL job results.
        """
        repo = inp["repo"]
        branch = inp["branch"]
        base_url = inp["base_url"]
        test_file = inp.get("test_file", "")

        # Validate before git sees them: reject `..`, leading `-`, and any
        # value outside the safe owner/name + ref charset (injected text).
        try:
            repo = validate_repo(repo)
            branch = validate_ref(branch)
        except InvalidGitRef as e:
            return {"success": False, "error": str(e)}

        if shutil.which("npm") is None or shutil.which("npx") is None:
            return await self._run_via_ci(repo, branch)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Clone the repo (hardened: -- before args, no transport but https,
            # no interactive credential prompt).
            clone_url = await self._build_clone_url(repo)
            returncode, stderr = await run_git_clone(clone_url, branch, tmpdir)
            if returncode != 0:
                return {"success": False, "error": f"Clone failed: {redact_secrets(stderr.decode())}"}

            # Install dependencies. --ignore-scripts blocks pre/post-install
            # lifecycle hooks (a supply-chain RCE vector via a malicious dep)
            # since we run untrusted repo dependencies here.
            install_proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                "--ignore-scripts",
                cwd=tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await install_proc.communicate()

            # Install Playwright browsers
            pw_install = await asyncio.create_subprocess_exec(
                "npx",
                "playwright",
                "install",
                "--with-deps",
                "chromium",
                cwd=tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await pw_install.communicate()

            # Run tests
            results_path = Path(tmpdir) / "test-results.json"
            cmd = [
                "npx",
                "playwright",
                "test",
                "--reporter=json",
                f"--output={results_path}",
            ]
            if test_file:
                cmd.append(test_file)

            env_vars = {"BASE_URL": base_url, "CI": "true"}
            test_proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**dict(__import__("os").environ), **env_vars},
            )
            stdout, stderr = await test_proc.communicate()

            # Parse results
            results: dict[str, Any] = {
                "success": test_proc.returncode == 0,
                "stdout": stdout.decode()[-2000:],  # Last 2000 chars
                "stderr": stderr.decode()[-1000:] if test_proc.returncode != 0 else "",
            }

            if results_path.exists():
                parsed_json = json.loads(results_path.read_text())
                results["json_results"] = parsed_json
                results["summary"] = self._summarize_results(parsed_json)

            return results

    _CI_POLL_SECONDS = 20.0
    _CI_MAX_WAIT_SECONDS = 600.0

    async def _run_via_ci(self, repo: str, branch: str) -> dict[str, Any]:
        """Execute tests through the repo's own CI: find the branch's latest
        workflow run (triggered by the agent's pushes), wait for completion,
        and return the real per-job results."""
        if self.scm is None:
            return {
                "success": False,
                "mode": "ci",
                "error": "no node toolchain in this runtime and no SCM client to read CI results",
            }
        deadline = time.monotonic() + self._CI_MAX_WAIT_SECONDS
        run: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                runs = await self.scm.get_pipeline_runs(repo, branch, limit=5)
            except Exception as e:  # noqa: BLE001
                return {"success": False, "mode": "ci", "error": f"could not list CI runs: {e}"}
            run = runs[0] if runs else None
            if run is None:
                # No workflow run yet — the push may still be propagating.
                await asyncio.sleep(self._CI_POLL_SECONDS)
                continue
            if str(run.get("status")) == "completed":
                break
            await asyncio.sleep(self._CI_POLL_SECONDS)

        if run is None:
            return {
                "success": False,
                "mode": "ci",
                "error": (
                    f"no CI workflow runs found for branch {branch!r} — ensure the repo has a "
                    "workflow that runs tests on push (e.g. .github/workflows/ci.yml) and that "
                    "your commits are pushed to that branch"
                ),
            }
        if str(run.get("status")) != "completed":
            return {
                "success": False,
                "mode": "ci",
                "error": f"CI run {run.get('id')} still {run.get('status')} after "
                f"{int(self._CI_MAX_WAIT_SECONDS)}s — check {run.get('html_url', '')}",
            }

        jobs: list[dict[str, Any]] = []
        try:
            jobs = await self.scm.get_pipeline_jobs(repo, run.get("id"))
        except Exception:  # noqa: BLE001 — conclusion alone is still useful
            logger.exception("CI job fetch failed for run %s", run.get("id"))

        failures = []
        passed = failed = 0
        for job in jobs:
            concl = str(job.get("conclusion") or "")
            if concl == "success":
                passed += 1
            elif concl in ("failure", "timed_out", "cancelled"):
                failed += 1
                failed_steps = [
                    s.get("name", "") for s in (job.get("steps") or []) if str(s.get("conclusion")) == "failure"
                ]
                failures.append(
                    {
                        "test": job.get("name", "job"),
                        "error": ("failed steps: " + ", ".join(failed_steps))
                        if failed_steps
                        else f"job conclusion: {concl}",
                        "url": job.get("html_url", ""),
                    }
                )
        success = str(run.get("conclusion")) == "success"
        return {
            "success": success,
            "mode": "ci",
            "workflow": run.get("name", ""),
            "run_url": run.get("html_url", ""),
            "summary": {
                "total": len(jobs) or 1,
                "passed": passed if jobs else (1 if success else 0),
                "failed": failed if jobs else (0 if success else 1),
                "skipped": 0,
                "failures": failures,
                "pass_rate": f"{(passed / len(jobs) * 100) if jobs else (100.0 if success else 0.0):.1f}%",
            },
        }

    async def _handle_parse_test_results(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Parse raw Playwright JSON results into a summary."""
        try:
            data = json.loads(inp["results_json"])
        except json.JSONDecodeError:
            return {"error": "Invalid JSON"}

        suites = data.get("suites", [])
        total = passed = failed = skipped = 0
        failures: list[dict[str, str]] = []

        def walk_suites(suite_list: list[dict[str, Any]]) -> None:
            nonlocal total, passed, failed, skipped
            for suite in suite_list:
                for spec in suite.get("specs", []):
                    for test in spec.get("tests", []):
                        for result in test.get("results", []):
                            total += 1
                            status = result.get("status", "")
                            if status == "passed":
                                passed += 1
                            elif status == "failed":
                                failed += 1
                                failures.append(
                                    {
                                        "test": spec.get("title", "unknown"),
                                        "error": str(result.get("error", {}).get("message", ""))[:500],
                                    }
                                )
                            elif status == "skipped":
                                skipped += 1
                walk_suites(suite.get("suites", []))

        walk_suites(suites)

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "failures": failures,
            "pass_rate": f"{(passed / total * 100) if total > 0 else 0:.1f}%",
        }

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

    def _summarize_results(self, data: dict[str, Any]) -> dict[str, Any]:
        suites = data.get("suites", [])
        total = passed = failed = skipped = 0
        failures: list[dict[str, str]] = []

        def walk_suites(suite_list: list[dict[str, Any]]) -> None:
            nonlocal total, passed, failed, skipped
            for suite in suite_list:
                for spec in suite.get("specs", []):
                    for test in spec.get("tests", []):
                        for result in test.get("results", []):
                            total += 1
                            status = result.get("status", "")
                            if status == "passed":
                                passed += 1
                            elif status == "failed":
                                failed += 1
                                failures.append(
                                    {
                                        "test": spec.get("title", "unknown"),
                                        "error": str(result.get("error", {}).get("message", ""))[:500],
                                    }
                                )
                            elif status == "skipped":
                                skipped += 1
                walk_suites(suite.get("suites", []))

        walk_suites(suites)
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "failures": failures,
            "pass_rate": f"{(passed / total * 100) if total > 0 else 0:.1f}%",
        }
