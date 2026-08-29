"""OpenAI Codex provider — Responses API for lightweight tasks + MCP server for sandbox tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from devai.services.redact import redact_secrets

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class CodexLiteProvider:
    """Lightweight Codex integration via OpenAI Responses API.

    Used for agents that need text generation but not sandbox execution
    (e.g., Product Director creating user stories).
    """

    def __init__(self, config: Settings) -> None:
        from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_required
        from devai.services.tracing import wrap_openai_client

        self._gateway_required = gateway_required(config)
        self.client = wrap_openai_client(
            AsyncOpenAI(
                api_key=config.openai_api_key,
                base_url=gateway_base_url(config, "openai", getattr(config, "openai_base_url", "")),
                default_headers={"x-devai-provider": "openai"} if gateway_required(config) else None,
            )
        )
        self.model = config.openai_model

    async def generate(
        self,
        prompt: str,
        system: str,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Generate a response using the OpenAI Responses API."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": prompt,
            "instructions": system,
        }
        if response_format:
            kwargs["text"] = {"format": response_format}
        if self._gateway_required:
            from devai.adapters.llm.gateway_routing import current_gateway_headers

            kwargs["extra_headers"] = current_gateway_headers("openai")

        result = await self.client.responses.create(**kwargs)
        return result.output_text


class CodexSandboxProvider:
    """Full Codex integration via MCP server subprocess.

    Used for agents that need sandboxed code execution
    (e.g., Staff Reviewer running linters and static analysis).
    """

    def __init__(self, config: Settings) -> None:
        from devai.adapters.llm.gateway_routing import gateway_required

        self.config = config
        self._gateway_required = gateway_required(config)
        self._process: subprocess.Popen[bytes] | None = None

    async def run_in_sandbox(
        self,
        prompt: str,
        repo_url: str,
        branch: str,
        sandbox_mode: str = "workspace-write",
    ) -> dict[str, Any]:
        """Run a Codex task in a sandboxed environment.

        Uses the Codex CLI's MCP server to execute tasks with full
        file system and shell access in a controlled sandbox.
        """
        # Build the Codex CLI command
        cmd = [
            "codex",
            "--quiet",
            f"--approval-mode={sandbox_mode}",
            prompt,
        ]

        gateway_headers: dict[str, str] = {}
        if self._gateway_required:
            from devai.adapters.llm.gateway_routing import current_gateway_headers

            gateway_headers = current_gateway_headers("openai")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._execute_codex, cmd, repo_url, branch, gateway_headers)
        return result

    def _execute_codex(
        self,
        cmd: list[str],
        repo_url: str,
        branch: str,
        gateway_headers: dict[str, str],
    ) -> dict[str, Any]:
        """Execute Codex CLI synchronously (runs in executor thread)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # Clone the repo and checkout the branch
            clone_result = subprocess.run(
                ["git", "clone", "--branch", branch, "--depth", "1", repo_url, tmpdir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if clone_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Failed to clone: {redact_secrets(clone_result.stderr or '')}",
                    "output": "",
                }

            # Run Codex in the cloned directory
            try:
                from devai.adapters.llm.gateway_routing import gateway_base_url

                base_url = gateway_base_url(
                    self.config,
                    "openai",
                    getattr(self.config, "openai_base_url", ""),
                )
                environment = {
                    "OPENAI_API_KEY": self.config.openai_api_key,
                    "OPENAI_BASE_URL": base_url,
                    "HOME": tmpdir,
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                }
                if self._gateway_required:
                    codex_home = Path(tmpdir) / ".codex"
                    codex_home.mkdir()
                    environment["CODEX_HOME"] = str(codex_home)
                    header_environment = {
                        "x-devai-tenant-id": "DEVAI_GATEWAY_TENANT_ID",
                        "x-devai-user-id": "DEVAI_GATEWAY_USER_ID",
                        "x-devai-run-id": "DEVAI_GATEWAY_RUN_ID",
                        "x-devai-agent": "DEVAI_GATEWAY_AGENT",
                        "x-devai-provider": "DEVAI_GATEWAY_PROVIDER",
                    }
                    mapped_headers = {
                        header: env_name
                        for header, env_name in header_environment.items()
                        if gateway_headers.get(header)
                    }
                    environment.update(
                        {env_name: gateway_headers[header] for header, env_name in mapped_headers.items()}
                    )
                    header_config = ", ".join(
                        f"{json.dumps(header)} = {json.dumps(env_name)}" for header, env_name in mapped_headers.items()
                    )
                    config_toml = (
                        f"model = {json.dumps(self.config.openai_model)}\n"
                        'model_provider = "devai_gateway"\n\n'
                        "[model_providers.devai_gateway]\n"
                        'name = "DevAI Agentic Gateway"\n'
                        f"base_url = {json.dumps(base_url)}\n"
                        'env_key = "OPENAI_API_KEY"\n'
                        f"env_http_headers = {{ {header_config} }}\n"
                    )
                    (codex_home / "config.toml").write_text(config_toml)

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=tmpdir,
                    timeout=600,  # 10 minute timeout
                    env=environment,
                )
                return {
                    "success": result.returncode == 0,
                    "output": result.stdout,
                    "error": redact_secrets(result.stderr) if result.returncode != 0 else "",
                }
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": "Codex execution timed out after 600s",
                    "output": "",
                }

    async def run_review(
        self,
        repo_url: str,
        branch: str,
        review_prompt: str,
    ) -> dict[str, Any]:
        """Run a code review in the Codex sandbox.

        Clones the branch, runs static analysis tools, and produces
        a structured review using the Codex agent.
        """
        full_prompt = f"""You are a Staff Software Engineer reviewing a pull request.

Repository: {repo_url}
Branch: {branch}

Review the code for:
1. Coding standards and best practices
2. Performance optimization opportunities
3. Security vulnerabilities (OWASP Top 10)
4. Code maintainability and readability

Run these analysis tools if available:
- For Python: ruff check, mypy, bandit
- For Go: golangci-lint, go vet
- For JavaScript/TypeScript: eslint, tsc --noEmit

{review_prompt}

Output your review as JSON with this structure:
{{
    "decision": "approved" or "changes_requested",
    "summary": "Overall review summary",
    "comments": [{{"file": "path", "line": 0, "body": "comment"}}],
    "security_issues": ["issue description"],
    "performance_issues": ["issue description"],
    "style_issues": ["issue description"]
}}"""

        return await self.run_in_sandbox(full_prompt, repo_url, branch)
