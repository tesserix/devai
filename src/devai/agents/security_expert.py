"""Security Expert Agent — comprehensive security gate in the ALM pipeline.

Runs AFTER code review and BEFORE build monitoring. Acts as a hard gate:
if critical security issues are found, the pipeline is blocked until fixed.

Scanning capabilities:
1. SAST — static analysis (bandit, gosec, eslint-security)
2. SCA — dependency vulnerability scanning (pip-audit, npm audit, govulncheck)
3. Secret detection — hardcoded credentials, API keys, tokens
4. Container security — Dockerfile best practices
5. OWASP Top 10 — compliance checklist against PR diff
6. SBOM — software bill of materials generation

Uses Claude for intelligent triage: distinguishes real threats from false positives,
prioritizes findings, and generates actionable fix recommendations.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor
from devai.tools.security_tools import SECURITY_TOOLS, SecurityToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# Security Expert gets read-only GitHub tools + all security tools
SECURITY_AGENT_TOOLS = [
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_get_pr_diff",
        "github_add_comment",
        "github_create_pr_review",
    }
] + SECURITY_TOOLS

SYSTEM_PROMPT = """You are a Principal Security Engineer and the security gate for the DevAI ALM pipeline.

Your job is to ensure that ALL code passing through this pipeline is safe, secure, and free from vulnerabilities before it reaches production.

## Your Responsibilities

1. **SAST Scan**: Run static analysis on the codebase using security_scan_sast
2. **Dependency Scan**: Check all dependencies for known CVEs using security_scan_dependencies
3. **Secret Detection**: Scan for hardcoded secrets using security_scan_secrets
4. **Container Security**: If Dockerfile exists, scan it using security_scan_container
5. **OWASP Compliance**: Check the PR diff against OWASP Top 10 using security_check_owasp
6. **SBOM Generation**: Generate software bill of materials using security_generate_sbom

## Process

1. First, get the PR diff with github_get_pr_diff to understand what changed
2. Detect the primary language from the repo tree (github_get_repo_tree)
3. Run ALL applicable security scans in sequence
4. Analyze findings — distinguish real threats from false positives
5. For CRITICAL/HIGH findings:
   - Block the pipeline (decision: BLOCK)
   - Post detailed review on the PR with specific fix instructions
6. For MEDIUM/LOW findings:
   - Allow pipeline to continue (decision: PASS_WITH_WARNINGS)
   - Post advisory comments
7. Generate the SBOM for compliance records

## Decision Criteria

**BLOCK** (pipeline stops) if ANY of:
- Hardcoded secrets/credentials found (verified or high-confidence)
- Critical CVE in dependencies with known exploits
- SQL injection, command injection, or code execution vulnerabilities
- Authentication/authorization bypass patterns
- Running container as root without justification

**PASS_WITH_WARNINGS** if:
- Only MEDIUM/LOW severity findings
- Known false positives from scanner
- Informational OWASP findings

**PASS** if:
- No findings at all

## Output Format

After running all scans, post a structured security review on the PR:

```markdown
## Security Scan Results

**Decision: PASS / PASS_WITH_WARNINGS / BLOCK**

### Summary
- SAST: X findings (Y critical)
- Dependencies: X CVEs found
- Secrets: X detected
- Container: X issues
- OWASP: X categories need review

### Critical Findings (if any)
- [file:line] Issue description + fix recommendation

### Advisory Findings (if any)
- [file:line] Issue description

### SBOM
- Total dependencies: X
```

Be thorough but pragmatic. Don't flag style issues as security — only genuine vulnerabilities.
Never miss a real vulnerability. When in doubt, flag it."""


class SecurityExpertAgent(BaseAgent):
    """Security gate agent — blocks pipeline on critical vulnerabilities."""

    name = "security_expert"
    subscribe_subject = "devai.pipeline.code_reviewed"
    publish_subject = "devai.pipeline.security_cleared"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Run comprehensive security scans and make pass/block decision."""
        claude = ClaudeProvider(self.config)
        github_tools = GitHubToolExecutor(self.github)
        security_tools = SecurityToolExecutor(self.github)

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            if tool_name.startswith("github_"):
                return await github_tools.execute(tool_name, tool_input)
            if tool_name.startswith("security_"):
                return await security_tools.execute(tool_name, tool_input)
            return f"Unknown tool: {tool_name}"

        repo = state.get("repo_full_name", "")
        pr_number = state.get("pr_number")
        branch = state.get("branch_name", "")

        if not pr_number or not branch:
            a2a.escalate(
                "engineering_manager",
                "Security Scan Blocked",
                "No PR or branch found — cannot perform security scan.",
            )
            return {
                "security_decision": "block",
                "security_summary": "No PR or branch found for scanning",
                "security_findings": [],
            }

        # Get active story context
        active_idx = state.get("active_story_index", 0)
        stories = state.get("stories", [])
        active_story = stories[active_idx] if active_idx < len(stories) else {}
        story_number = active_story.get("number", "?")
        story_title = active_story.get("title", "")

        # Check for messages from other agents
        inbox_context = a2a.format_inbox_context()
        memory_context = state.get("memory_context", "")

        user_message = f"""Repository: {repo}
PR: #{pr_number}
Branch: {branch}
Story: #{story_number} — {story_title}

{inbox_context}

## Relevant Memory From Past Runs
{memory_context or "(none)"}

Run a comprehensive security scan of this PR (Story #{story_number}). Follow the process:
1. Get the PR diff to understand changes
2. Explore the repo tree to detect the primary language
3. Run SAST, dependency scan, secret detection, container scan, and OWASP check
4. Analyze all findings and make a PASS/PASS_WITH_WARNINGS/BLOCK decision
5. Post a detailed security review on the PR
6. Generate the SBOM

Be thorough. This is the last security gate before production."""

        # Inject skill profile + governance — gives the security agent
        # stack-specific OWASP/CWE guidance instead of generic checks.
        from devai.agents.skills import get_skill_profile

        system = SYSTEM_PROMPT
        profile = get_skill_profile(state.get("skill_profile_name"))
        system += "\n\n" + profile.render_for_security()

        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=SECURITY_AGENT_TOOLS,
            tool_executor=tool_executor,
            max_iterations=self.config.claude_max_iterations_review,
        )

        # Parse decision from result
        decision = self._parse_decision(result_text)

        # A2A communication based on decision — include story context
        if decision == "block":
            a2a.escalate(
                "senior_developer",
                f"SECURITY BLOCK — Story #{story_number}",
                f"Security scan of PR #{pr_number} (Story #{story_number}) found critical issues. "
                f"Pipeline is blocked until these are fixed.\n\n{result_text[:500]}",
            )
            a2a.notify(
                "engineering_manager",
                f"Security Gate BLOCKED — Story #{story_number}",
                f"PR #{pr_number} (Story #{story_number}) blocked by security scan.",
            )
            a2a.notify(
                "staff_reviewer",
                f"Security Issues — Story #{story_number}",
                f"Security scan found issues in PR #{pr_number} (Story #{story_number}).",
            )
        elif decision == "pass_with_warnings":
            a2a.notify(
                "qa_tester",
                f"Story #{story_number} Security Cleared (with warnings)",
                f"PR #{pr_number} (Story #{story_number}) passed security with advisory warnings.",
            )
        else:
            a2a.handoff(
                "qa_tester",
                f"Story #{story_number} Security Cleared",
                f"PR #{pr_number} (Story #{story_number}) passed all security scans. Proceed to testing.",
            )
            a2a.broadcast(
                f"Story #{story_number} Security Scan Passed",
                f"PR #{pr_number} (Story #{story_number}) cleared security gate.",
                exclude=["qa_tester"],
            )

        return {
            "security_decision": decision,
            "security_summary": result_text,
            "security_findings": [],  # Full findings in the summary text
        }

    def _parse_decision(self, result_text: str) -> str:
        """Extract PASS/PASS_WITH_WARNINGS/BLOCK from agent output."""
        text_upper = result_text.upper()

        if "DECISION: BLOCK" in text_upper or "**BLOCK**" in text_upper:
            return "block"
        if "PASS_WITH_WARNINGS" in text_upper or "PASS WITH WARNINGS" in text_upper:
            return "pass_with_warnings"
        if "DECISION: PASS" in text_upper or "**PASS**" in text_upper:
            return "pass"

        # If we can't determine, be conservative
        if any(kw in text_upper for kw in ["CRITICAL", "HIGH SEVERITY", "HARDCODED SECRET", "INJECTION"]):
            return "block"

        return "pass_with_warnings"
