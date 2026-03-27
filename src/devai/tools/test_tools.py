"""Playwright test execution tools for the QA Tester agent."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_playwright_test",
        "description": "Run Playwright E2E tests headlessly and return results. Tests must be written first using github_commit_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch with test files"},
                "test_file": {"type": "string", "description": "Specific test file to run (optional, runs all if omitted)"},
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

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        result = await handler(tool_input)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)

    async def _handle_run_playwright_test(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Clone repo, install deps, run Playwright tests headlessly."""
        repo = inp["repo"]
        branch = inp["branch"]
        base_url = inp["base_url"]
        test_file = inp.get("test_file", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Clone the repo
            clone_proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--branch", branch, "--depth", "1",
                f"https://github.com/{repo}.git", tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await clone_proc.communicate()
            if clone_proc.returncode != 0:
                return {"success": False, "error": f"Clone failed: {stderr.decode()}"}

            # Install dependencies
            install_proc = await asyncio.create_subprocess_exec(
                "npm", "install",
                cwd=tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await install_proc.communicate()

            # Install Playwright browsers
            pw_install = await asyncio.create_subprocess_exec(
                "npx", "playwright", "install", "--with-deps", "chromium",
                cwd=tmpdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await pw_install.communicate()

            # Run tests
            results_path = Path(tmpdir) / "test-results.json"
            cmd = [
                "npx", "playwright", "test",
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
                results["json_results"] = json.loads(results_path.read_text())

            return results

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
                                failures.append({
                                    "test": spec.get("title", "unknown"),
                                    "error": str(result.get("error", {}).get("message", ""))[:500],
                                })
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
