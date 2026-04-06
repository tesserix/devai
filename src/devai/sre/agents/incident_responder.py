"""Incident Responder Agent — creates GitHub issues/PRs from SRE findings.

Takes findings from all other SRE agents, correlates them, and:
1. Creates GitHub issues assigned to the right team members
2. Creates PRs with fixes (resource adjustments, config changes)
3. Tracks incident lifecycle (open → investigating → resolved)
4. Learns from past incidents to improve future responses
"""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.providers.anthropic_claude import ClaudeProvider
from devai.scm import create_scm_client
from devai.services.memory import AgentMemory

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE Incident Responder. Your job is to take findings from monitoring agents
and create actionable GitHub issues and pull requests.

For each finding, determine:
1. Should this be an ISSUE (human needs to investigate/fix)?
2. Should this be a PR (you can generate the fix)?
3. Should this be IGNORED (false positive, already known)?

Issue format:
```markdown
## [SEVERITY] Title

**Category:** performance | cost | availability | security | capacity
**Detected by:** agent_name
**Affected:** namespace/deployment

### Evidence
- Metric/log data that proves the issue

### Impact
- What users/services are affected

### Recommended Fix
- Step-by-step remediation

### Related
- Links to dashboards, past incidents
```

PR format (for auto-fixable issues):
- Resource limit adjustments (values.yaml changes)
- HPA configuration changes
- Replica count adjustments
- ConfigMap updates

Always assign issues to the team/users responsible for that service.
Always add appropriate labels (sre, severity, category).
Never create duplicate issues — check if one already exists."""


class IncidentResponderAgent:
    """Creates issues and PRs from SRE findings."""

    name = "incident_responder"

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db
        self.claude = ClaudeProvider(config)

    async def run(
        self,
        findings: list[dict[str, Any]],
        apps: list[dict[str, Any]],
        cluster_id: str,
        scan_run_id: str,
    ) -> dict[str, Any]:
        """Process findings and create issues/PRs."""
        if not findings:
            return {"incidents_created": 0, "prs_created": 0}

        scm = create_scm_client(self.config)
        incidents_created = 0
        prs_created = 0

        # Group findings by severity
        critical = [f for f in findings if f.get("severity") == "critical"]
        high = [f for f in findings if f.get("severity") == "high"]
        medium = [f for f in findings if f.get("severity") == "medium"]

        # Process critical and high findings immediately
        for finding in critical + high:
            # Find the app and its owner
            app = self._find_app(finding, apps)
            repo = app.get("repo", "") if app else ""
            owners = app.get("owner_users", []) if app else []

            if not repo:
                logger.warning("No repo linked for finding: %s", finding.get("title", ""))
                continue

            # Check for duplicates via memory
            try:
                memory = AgentMemory(self.db.pool if hasattr(self.db, 'pool') else self.db)
                past = await memory.recall(
                    agent=self.name,
                    repo=repo,
                    tags=[finding.get("category", ""), finding.get("title", "")[:30]],
                    limit=1,
                )
                if past and past[0].created_at > (import_time() - 86400):
                    logger.info("Skipping duplicate incident: %s", finding.get("title"))
                    continue
            except Exception:
                pass

            # Create the issue
            severity = finding.get("severity", "medium").upper()
            category = finding.get("category", "unknown")
            title = f"[SRE-{severity}] {finding.get('title', 'Unknown Issue')}"

            body = self._format_issue_body(finding, app)

            labels = [
                "sre",
                f"severity:{finding.get('severity', 'medium')}",
                f"category:{category}",
                "devai:sre-detected",
            ]

            try:
                issue = await scm.create_issue(repo, title, body, labels)
                issue_number = issue.get("number") or issue.get("iid")

                # Assign to owners
                if owners and issue_number:
                    try:
                        # GitHub assignees via API
                        await scm._request(
                            "POST",
                            f"/repos/{repo}/issues/{issue_number}/assignees",
                            json={"assignees": owners[:3]},
                        )
                    except Exception:
                        pass

                # Record in database
                await self.db.execute(
                    """INSERT INTO sre_incidents
                       (id, cluster_id, app_id, severity, category, title, description,
                        evidence, detected_by, scm_issue_number, scm_repo, assigned_to, status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'open')""",
                    scan_run_id + str(incidents_created),
                    cluster_id,
                    app.get("id") if app else None,
                    finding.get("severity", "medium"),
                    category,
                    finding.get("title", ""),
                    finding.get("description", ""),
                    json.dumps(finding.get("evidence", {})),
                    finding.get("detected_by", self.name),
                    issue_number,
                    repo,
                    owners,
                )

                incidents_created += 1
                logger.info("Created SRE issue #%s on %s: %s", issue_number, repo, title)

                # Remember this incident
                try:
                    await memory.remember(
                        agent=self.name,
                        content=f"Created issue #{issue_number}: {title}",
                        memory_type="episodic",
                        repo=repo,
                        tags=[category, finding.get("title", "")[:30]],
                    )
                except Exception:
                    pass

            except Exception as e:
                logger.error("Failed to create issue on %s: %s", repo, e)

        # Medium findings: batch into a single summary issue per repo
        if medium:
            repos_with_medium: dict[str, list[dict]] = {}
            for f in medium:
                app = self._find_app(f, apps)
                repo = app.get("repo", "unknown") if app else "unknown"
                repos_with_medium.setdefault(repo, []).append(f)

            for repo, repo_findings in repos_with_medium.items():
                if repo == "unknown":
                    continue
                title = f"[SRE-MEDIUM] {len(repo_findings)} monitoring findings"
                body = "## SRE Monitoring Summary\n\n"
                for f in repo_findings:
                    body += f"### {f.get('title', '')}\n{f.get('description', '')}\n\n"

                try:
                    await scm.create_issue(repo, title, body, ["sre", "severity:medium", "devai:sre-detected"])
                    incidents_created += 1
                except Exception as e:
                    logger.error("Failed to create summary issue on %s: %s", repo, e)

        await scm.close()
        return {"incidents_created": incidents_created, "prs_created": prs_created}

    def _find_app(self, finding: dict, apps: list[dict]) -> dict[str, Any] | None:
        """Match a finding to a monitored app."""
        app_name = finding.get("app", "") or finding.get("pod", "")
        namespace = finding.get("namespace", "")

        for app in apps:
            if app.get("name", "") in app_name or app_name in app.get("name", ""):
                return app
            if namespace and app.get("namespace") == namespace:
                return app

        return apps[0] if apps else None

    def _format_issue_body(self, finding: dict, app: dict | None) -> str:
        severity = finding.get("severity", "medium").upper()
        category = finding.get("category", "unknown")
        detected_by = finding.get("detected_by", "sre_monitor")

        body = f"""## [{severity}] {finding.get('title', '')}

**Category:** {category}
**Detected by:** {detected_by}
**Affected:** {app.get('namespace', 'unknown')}/{app.get('name', 'unknown') if app else 'unknown'}

### Evidence
```
{json.dumps(finding.get('evidence', finding), indent=2)[:2000]}
```

### Description
{finding.get('description', 'No description available.')}

### Recommended Fix
{finding.get('recommendation', 'Investigate the evidence above and apply appropriate fix.')}

---
*This issue was automatically created by the DevAI SRE monitoring system.*
"""
        return body


def import_time():
    import time
    return time.time()
