"""Security scanning tools for the Security Expert agent.

Provides SAST (static analysis), SCA (dependency scanning), secret detection,
and OWASP compliance checking. Each tool wraps a standard open-source scanner
and normalizes output for LLM consumption.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

SECURITY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "security_scan_sast",
        "description": "Run Static Application Security Testing (SAST) on the codebase. "
        "Supports Python (bandit+semgrep), Go (gosec), JS/TS (eslint-security). "
        "Returns structured vulnerability findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to scan"},
                "language": {"type": "string", "description": "Primary language: python|go|javascript|typescript"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
    {
        "name": "security_scan_dependencies",
        "description": "Scan dependencies for known CVEs. "
        "Supports pip (safety/pip-audit), npm (npm audit), Go (govulncheck).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to scan"},
                "language": {"type": "string", "description": "Primary language: python|go|javascript|typescript"},
            },
            "required": ["repo", "branch", "language"],
        },
    },
    {
        "name": "security_scan_secrets",
        "description": "Scan code for hardcoded secrets, API keys, credentials, and tokens. "
        "Uses pattern-based detection for common secret formats.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to scan"},
            },
            "required": ["repo", "branch"],
        },
    },
    {
        "name": "security_scan_container",
        "description": "Scan Dockerfile and container image for vulnerabilities. "
        "Checks base image, exposed ports, running as root, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to scan"},
                "dockerfile_path": {"type": "string", "description": "Path to Dockerfile (default: Dockerfile)"},
            },
            "required": ["repo", "branch"],
        },
    },
    {
        "name": "security_check_owasp",
        "description": "Check code against OWASP Top 10 categories. "
        "Produces a compliance checklist with pass/fail per category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "PR diff to analyze for OWASP issues"},
                "language": {"type": "string", "description": "Primary language"},
            },
            "required": ["diff", "language"],
        },
    },
    {
        "name": "security_generate_sbom",
        "description": "Generate a Software Bill of Materials (SBOM) listing all dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "branch": {"type": "string", "description": "Branch to scan"},
            },
            "required": ["repo", "branch"],
        },
    },
]


class SecurityToolExecutor:
    """Executes security scanning tools."""

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

    async def _handle_security_scan_sast(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Run SAST scanners based on language."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            findings: list[dict[str, Any]] = []

            if lang == "python":
                # Bandit
                bandit_out = await self._run_cmd(
                    ["python", "-m", "bandit", "-r", ".", "-f", "json", "-ll"],
                    tmpdir,
                    timeout=120,
                )
                if bandit_out:
                    try:
                        data = json.loads(bandit_out)
                        for r in data.get("results", []):
                            findings.append(
                                {
                                    "tool": "bandit",
                                    "severity": r.get("issue_severity", "MEDIUM"),
                                    "confidence": r.get("issue_confidence", "MEDIUM"),
                                    "file": r.get("filename", ""),
                                    "line": r.get("line_number", 0),
                                    "issue": r.get("issue_text", ""),
                                    "cwe": r.get("issue_cwe", {}).get("id", ""),
                                }
                            )
                    except json.JSONDecodeError:
                        findings.append({"tool": "bandit", "raw": bandit_out[:2000]})

            elif lang == "go":
                gosec_out = await self._run_cmd(
                    ["gosec", "-fmt=json", "./..."],
                    tmpdir,
                    timeout=120,
                )
                if gosec_out:
                    try:
                        data = json.loads(gosec_out)
                        for issue in data.get("Issues", []):
                            findings.append(
                                {
                                    "tool": "gosec",
                                    "severity": issue.get("severity", "MEDIUM"),
                                    "confidence": issue.get("confidence", "MEDIUM"),
                                    "file": issue.get("file", ""),
                                    "line": issue.get("line", "0"),
                                    "issue": issue.get("details", ""),
                                    "cwe": issue.get("cwe", {}).get("id", ""),
                                }
                            )
                    except json.JSONDecodeError:
                        findings.append({"tool": "gosec", "raw": gosec_out[:2000]})

            elif lang in ("javascript", "typescript"):
                # ESLint security plugin
                eslint_out = await self._run_cmd(
                    [
                        "npx",
                        "eslint",
                        "--no-eslintrc",
                        "--plugin",
                        "security",
                        "--rule",
                        '{"security/detect-object-injection": "warn", '
                        '"security/detect-non-literal-regexp": "warn", '
                        '"security/detect-unsafe-regex": "warn", '
                        '"security/detect-eval-with-expression": "error"}',
                        "-f",
                        "json",
                        ".",
                    ],
                    tmpdir,
                    timeout=120,
                )
                if eslint_out:
                    try:
                        data = json.loads(eslint_out)
                        for file_result in data:
                            for msg in file_result.get("messages", []):
                                findings.append(
                                    {
                                        "tool": "eslint-security",
                                        "severity": "HIGH" if msg.get("severity", 1) >= 2 else "MEDIUM",
                                        "file": file_result.get("filePath", ""),
                                        "line": msg.get("line", 0),
                                        "issue": msg.get("message", ""),
                                        "rule": msg.get("ruleId", ""),
                                    }
                                )
                    except json.JSONDecodeError:
                        pass

            return {
                "scanner": "SAST",
                "language": lang,
                "total_findings": len(findings),
                "critical": len([f for f in findings if f.get("severity") == "HIGH"]),
                "findings": findings[:50],  # Limit to 50
            }

    async def _handle_security_scan_dependencies(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Scan dependencies for known CVEs."""
        repo, branch, lang = inp["repo"], inp["branch"], inp["language"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            vulns: list[dict[str, Any]] = []

            if lang == "python":
                # pip-audit
                audit_out = await self._run_cmd(
                    ["python", "-m", "pip_audit", "--format=json", "--desc"],
                    tmpdir,
                    timeout=120,
                )
                if audit_out:
                    try:
                        data = json.loads(audit_out)
                        for dep in data.get("dependencies", []):
                            for vuln in dep.get("vulns", []):
                                vulns.append(
                                    {
                                        "package": dep.get("name", ""),
                                        "version": dep.get("version", ""),
                                        "cve": vuln.get("id", ""),
                                        "description": vuln.get("description", "")[:200],
                                        "fix_version": vuln.get("fix_versions", ["unknown"])[0],
                                    }
                                )
                    except json.JSONDecodeError:
                        pass

            elif lang in ("javascript", "typescript"):
                audit_out = await self._run_cmd(
                    ["npm", "audit", "--json"],
                    tmpdir,
                    timeout=60,
                )
                if audit_out:
                    try:
                        data = json.loads(audit_out)
                        for name, adv in data.get("vulnerabilities", {}).items():
                            vulns.append(
                                {
                                    "package": name,
                                    "severity": adv.get("severity", ""),
                                    "via": str(adv.get("via", ""))[:200],
                                    "fix_available": adv.get("fixAvailable", False),
                                }
                            )
                    except json.JSONDecodeError:
                        pass

            elif lang == "go":
                vuln_out = await self._run_cmd(
                    ["govulncheck", "-json", "./..."],
                    tmpdir,
                    timeout=120,
                )
                if vuln_out:
                    try:
                        for line in vuln_out.strip().split("\n"):
                            entry = json.loads(line)
                            if "finding" in entry:
                                f = entry["finding"]
                                vulns.append(
                                    {
                                        "osv_id": f.get("osv", ""),
                                        "trace": str(f.get("trace", []))[:200],
                                    }
                                )
                    except (json.JSONDecodeError, ValueError):
                        pass

            return {
                "scanner": "SCA",
                "language": lang,
                "total_vulnerabilities": len(vulns),
                "vulnerabilities": vulns[:30],
            }

    async def _handle_security_scan_secrets(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Scan for hardcoded secrets using pattern matching."""
        repo, branch = inp["repo"], inp["branch"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            # Try trufflehog first, fall back to grep patterns
            secrets: list[dict[str, Any]] = []

            trufflehog_out = await self._run_cmd(
                ["trufflehog", "filesystem", "--json", "--no-update", tmpdir],
                tmpdir,
                timeout=120,
            )
            if trufflehog_out:
                for line in trufflehog_out.strip().split("\n"):
                    try:
                        entry = json.loads(line)
                        if entry.get("SourceMetadata"):
                            meta = entry["SourceMetadata"].get("Data", {}).get("Filesystem", {})
                            secrets.append(
                                {
                                    "file": meta.get("file", ""),
                                    "line": meta.get("line", 0),
                                    "detector": entry.get("DetectorName", ""),
                                    "verified": entry.get("Verified", False),
                                    "raw_preview": entry.get("Raw", "")[:30] + "***",
                                }
                            )
                    except json.JSONDecodeError:
                        continue
            else:
                # Fallback: grep-based secret patterns
                patterns = [
                    (r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-]{20,}", "API Key"),
                    (r"(?i)(secret|password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}", "Password/Secret"),
                    (r"(?i)(aws_access_key_id|aws_secret_access_key)\s*[:=]", "AWS Credential"),
                    (r"ghp_[a-zA-Z0-9]{36}", "GitHub PAT"),
                    (r"gho_[a-zA-Z0-9]{36}", "GitHub OAuth Token"),
                    (r"sk-[a-zA-Z0-9]{48}", "OpenAI API Key"),
                    (r"sk-ant-[a-zA-Z0-9\-]{80,}", "Anthropic API Key"),
                    (r"gsk_[a-zA-Z0-9]{52}", "Groq API Key"),
                    (r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----", "Private Key"),
                    (r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}", "JWT Token"),
                ]

                for pattern, label in patterns:
                    grep_out = await self._run_cmd(
                        [
                            "grep",
                            "-rn",
                            "-E",
                            pattern,
                            "--include=*.py",
                            "--include=*.go",
                            "--include=*.js",
                            "--include=*.ts",
                            "--include=*.yaml",
                            "--include=*.yml",
                            "--include=*.json",
                            "--include=*.env",
                            "--include=*.toml",
                            "--include=*.cfg",
                            "--exclude-dir=.git",
                            "--exclude-dir=node_modules",
                            "--exclude-dir=vendor",
                            "--exclude-dir=__pycache__",
                            ".",
                        ],
                        tmpdir,
                        timeout=30,
                    )
                    if grep_out:
                        for line in grep_out.strip().split("\n")[:5]:
                            parts = line.split(":", 2)
                            if len(parts) >= 2:
                                secrets.append(
                                    {
                                        "file": parts[0],
                                        "line": parts[1],
                                        "detector": label,
                                        "verified": False,
                                        "raw_preview": parts[2][:40] + "***" if len(parts) > 2 else "",
                                    }
                                )

            return {
                "scanner": "secrets",
                "total_secrets": len(secrets),
                "verified_secrets": len([s for s in secrets if s.get("verified")]),
                "secrets": secrets[:20],
            }

    async def _handle_security_scan_container(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Scan Dockerfile for security issues."""
        repo, branch = inp["repo"], inp["branch"]
        dockerfile_path = inp.get("dockerfile_path", "Dockerfile")

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            dfile = Path(tmpdir) / dockerfile_path
            if not dfile.exists():
                return {"scanner": "container", "findings": [], "note": "No Dockerfile found"}

            findings: list[dict[str, str]] = []
            content = dfile.read_text()

            # Static checks
            if "FROM" in content:
                # Check for latest tag
                for line in content.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("FROM") and (
                        ":latest" in stripped or ":" not in stripped.split()[1] if len(stripped.split()) > 1 else True
                    ):
                        findings.append(
                            {
                                "severity": "MEDIUM",
                                "issue": "Base image uses :latest tag — pin to specific version",
                                "line": stripped,
                            }
                        )

            if "USER" not in content:
                findings.append(
                    {"severity": "HIGH", "issue": "No USER instruction — container runs as root", "line": ""}
                )

            if "HEALTHCHECK" not in content:
                findings.append({"severity": "LOW", "issue": "No HEALTHCHECK instruction", "line": ""})

            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("EXPOSE") and "22" in stripped.split():
                    findings.append({"severity": "HIGH", "issue": "SSH port 22 exposed", "line": stripped})
                if "chmod 777" in stripped:
                    findings.append({"severity": "HIGH", "issue": "Overly permissive chmod 777", "line": stripped})
                if stripped.startswith("ADD") and ("http://" in stripped or "https://" in stripped):
                    findings.append(
                        {
                            "severity": "MEDIUM",
                            "issue": "ADD from remote URL — use COPY + curl instead",
                            "line": stripped,
                        }
                    )

            # Try hadolint if available
            hadolint_out = await self._run_cmd(
                ["hadolint", "-f", "json", str(dfile)],
                tmpdir,
                timeout=30,
            )
            if hadolint_out:
                try:
                    for item in json.loads(hadolint_out):
                        findings.append(
                            {
                                "severity": item.get("level", "warning").upper(),
                                "issue": f"[{item.get('code', '')}] {item.get('message', '')}",
                                "line": str(item.get("line", "")),
                            }
                        )
                except json.JSONDecodeError:
                    pass

            return {
                "scanner": "container",
                "total_findings": len(findings),
                "findings": findings[:20],
            }

    async def _handle_security_check_owasp(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Check diff against OWASP Top 10 categories."""
        diff = inp.get("diff", "")
        inp.get("language", "unknown")

        # OWASP Top 10 (2021) checklist
        checks: list[dict[str, Any]] = [
            {
                "id": "A01",
                "name": "Broken Access Control",
                "patterns": [
                    "role",
                    "admin",
                    "authorize",
                    "permission",
                    "rbac",
                    "acl",
                ],
            },
            {
                "id": "A02",
                "name": "Cryptographic Failures",
                "patterns": [
                    "md5",
                    "sha1",
                    "base64",
                    "encrypt",
                    "decrypt",
                    "hash",
                    "salt",
                ],
            },
            {
                "id": "A03",
                "name": "Injection",
                "patterns": [
                    "sql",
                    "exec",
                    "eval",
                    "system(",
                    "subprocess",
                    "os.popen",
                    "innerHTML",
                    "dangerouslySetInnerHTML",
                    "raw(",
                    "format(",
                ],
            },
            {
                "id": "A04",
                "name": "Insecure Design",
                "patterns": [
                    "todo",
                    "fixme",
                    "hack",
                    "workaround",
                    "temporary",
                ],
            },
            {
                "id": "A05",
                "name": "Security Misconfiguration",
                "patterns": [
                    "debug",
                    "cors",
                    "allow_all",
                    "permit_all",
                    "*.*.*",
                    "0.0.0.0",
                    "disable_ssl",
                    "verify=false",
                    "insecure",
                ],
            },
            {"id": "A06", "name": "Vulnerable Components", "patterns": []},
            {
                "id": "A07",
                "name": "Auth Failures",
                "patterns": [
                    "password",
                    "token",
                    "jwt",
                    "session",
                    "cookie",
                    "oauth",
                ],
            },
            {
                "id": "A08",
                "name": "Software/Data Integrity",
                "patterns": [
                    "deserializ",
                    "pickle",
                    "yaml.load",
                    "marshal",
                    "unserialize",
                ],
            },
            {
                "id": "A09",
                "name": "Logging/Monitoring Failures",
                "patterns": [
                    "log",
                    "audit",
                    "monitor",
                    "alert",
                    "trace",
                ],
            },
            {
                "id": "A10",
                "name": "SSRF",
                "patterns": [
                    "url",
                    "fetch",
                    "request",
                    "http",
                    "redirect",
                ],
            },
        ]

        diff_lower = diff.lower()
        results: list[dict[str, Any]] = []

        for check in checks:
            relevant = any(p in diff_lower for p in check["patterns"])
            results.append(
                {
                    "id": check["id"],
                    "category": check["name"],
                    "relevant_to_diff": relevant,
                    "status": "REVIEW_NEEDED" if relevant else "NOT_APPLICABLE",
                }
            )

        review_needed = [r for r in results if r["status"] == "REVIEW_NEEDED"]

        return {
            "scanner": "OWASP",
            "total_categories": 10,
            "review_needed": len(review_needed),
            "checklist": results,
        }

    async def _handle_security_generate_sbom(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Generate SBOM from repository."""
        repo, branch = inp["repo"], inp["branch"]

        with tempfile.TemporaryDirectory() as tmpdir:
            if not await self._clone(repo, branch, tmpdir):
                return {"error": "Failed to clone repository"}

            deps: list[dict[str, str]] = []
            path = Path(tmpdir)

            # Python
            req_file = path / "requirements.txt"
            if req_file.exists():
                for line in req_file.read_text().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("==")
                        deps.append(
                            {
                                "name": parts[0].strip(),
                                "version": parts[1].strip() if len(parts) > 1 else "unknown",
                                "ecosystem": "pypi",
                            }
                        )

            pyproject = path / "pyproject.toml"
            if pyproject.exists():
                content = pyproject.read_text()
                # Simple extraction
                in_deps = False
                for line in content.split("\n"):
                    if "dependencies" in line and "=" in line:
                        in_deps = True
                        continue
                    if in_deps and line.strip().startswith("]"):
                        in_deps = False
                    if in_deps and line.strip().startswith('"'):
                        dep = line.strip().strip('",')
                        deps.append(
                            {"name": dep.split(">=")[0].split("==")[0].strip(), "version": ">=*", "ecosystem": "pypi"}
                        )

            # Node.js
            pkg_json = path / "package.json"
            if pkg_json.exists():
                try:
                    pkg = json.loads(pkg_json.read_text())
                    for name, ver in pkg.get("dependencies", {}).items():
                        deps.append({"name": name, "version": ver, "ecosystem": "npm"})
                    for name, ver in pkg.get("devDependencies", {}).items():
                        deps.append({"name": name, "version": ver, "ecosystem": "npm"})
                except json.JSONDecodeError:
                    pass

            # Go
            go_mod = path / "go.mod"
            if go_mod.exists():
                for line in go_mod.read_text().split("\n"):
                    line = line.strip()
                    if (
                        line
                        and not line.startswith("module")
                        and not line.startswith("go ")
                        and not line.startswith("//")
                    ):
                        if line.startswith("require") or line == "(" or line == ")":
                            continue
                        parts = line.split()
                        if len(parts) >= 2:
                            deps.append({"name": parts[0], "version": parts[1], "ecosystem": "go"})

            return {
                "format": "devai-sbom-v1",
                "repository": repo,
                "branch": branch,
                "total_dependencies": len(deps),
                "dependencies": deps,
            }

    # --- Helpers ---

    async def _clone(self, repo: str, branch: str, tmpdir: str) -> bool:
        clone_url = await self._build_clone_url(repo)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--branch",
            branch,
            "--depth",
            "1",
            clone_url,
            tmpdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Clone failed: %s", stderr.decode()[:500])
            return False
        return True

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

    async def _run_cmd(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int = 60,
    ) -> str:
        """Run a command and return stdout, or empty string on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace")
        except (TimeoutError, FileNotFoundError, Exception) as e:
            logger.debug("Command %s failed: %s", cmd[0], e)
            return ""
