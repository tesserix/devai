"""Incident Responder Agent — creates GitHub issues/PRs from SRE findings.

Takes findings from all other SRE agents, correlates them, and:
1. Creates GitHub issues assigned to the right team members
2. Creates PRs with fixes (resource adjustments, config changes)
3. Tracks incident lifecycle (open → investigating → resolved)
4. Learns from past incidents to improve future responses
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider
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
        self.openai = OpenAIProvider(config)

    async def run(
        self,
        findings: list[dict[str, Any]],
        apps: list[dict[str, Any]],
        cluster_id: str,
        scan_run_id: str,
    ) -> dict[str, Any]:
        """Process findings and create issues/PRs.

        Every actionable finding is persisted to ``sre_incidents`` regardless
        of whether a GitHub issue can be filed — that way the dashboard
        Incidents page reflects everything the SRE pipeline saw, even when
        the SCM repo is unknown or the API call fails.
        """
        if not findings:
            return {"incidents_created": 0, "prs_created": 0}

        scm = None
        with contextlib.suppress(Exception):
            scm = create_scm_client(self.config)

        incidents_created = 0
        prs_created = 0

        # Group findings by severity
        critical = [f for f in findings if f.get("severity") == "critical"]
        high = [f for f in findings if f.get("severity") == "high"]
        medium = [f for f in findings if f.get("severity") == "medium"]

        # Process every actionable finding individually
        for finding in critical + high + medium:
            try:
                created = await self._record_incident(
                    finding=finding,
                    apps=apps,
                    cluster_id=cluster_id,
                    scm=scm,
                )
                if created:
                    incidents_created += 1
            except Exception as e:
                logger.error("Failed to record incident '%s': %s", finding.get("title", ""), e)

        # Optional: also create a single summary issue per repo for the
        # medium-severity batch — purely additive, doesn't double-count.
        if medium and scm is not None:
            repos_with_medium: dict[str, list[dict]] = {}
            for f in medium:
                app = self._find_app(f, apps)
                repo = self._resolve_repo(f, app)
                if repo:
                    repos_with_medium.setdefault(repo, []).append(f)

            for repo, repo_findings in repos_with_medium.items():
                title = f"[SRE-MEDIUM] {len(repo_findings)} monitoring findings"
                body = "## SRE Monitoring Summary\n\n"
                for f in repo_findings:
                    body += f"### {f.get('title', '')}\n{f.get('description', '')}\n\n"
                with contextlib.suppress(Exception):
                    if hasattr(scm, "create_issue_idempotent"):
                        await scm.create_issue_idempotent(
                            repo,
                            title,
                            body,
                            ["sre", "severity:medium", "devai:sre-detected"],
                            dedupe_labels=["devai:sre-detected"],
                        )
                    else:
                        await scm.create_issue(
                            repo,
                            title,
                            body,
                            ["sre", "severity:medium", "devai:sre-detected"],
                        )

        if scm is not None:
            with contextlib.suppress(Exception):
                await scm.close()

        logger.info(
            "Incident responder: scan=%s persisted=%d findings (cluster=%s)",
            scan_run_id,
            incidents_created,
            cluster_id,
        )
        return {"incidents_created": incidents_created, "prs_created": prs_created}

    async def _record_incident(
        self,
        finding: dict[str, Any],
        apps: list[dict[str, Any]],
        cluster_id: str,
        scm: Any,
    ) -> bool:
        """Insert a sre_incidents row and (optionally) file a GitHub issue."""
        from ulid import ULID

        app = self._find_app(finding, apps)
        repo = self._resolve_repo(finding, app)
        owners = (app.get("owner_users") if app else []) or []
        severity = finding.get("severity", "medium")
        category = finding.get("category", "unknown")
        incident_id = str(ULID())

        # 1. Persist the incident first — always — so the Incidents page
        #    sees it even if SCM is offline.
        try:
            await self.db.pool.execute(
                """INSERT INTO sre_incidents
                   (id, cluster_id, app_id, severity, category, title, description,
                    evidence, detected_by, scm_repo, assigned_to, status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'open')""",
                incident_id,
                cluster_id,
                app.get("id") if app else None,
                severity,
                category,
                finding.get("title", "Untitled finding"),
                finding.get("description", ""),
                json.dumps(finding.get("evidence", {})),
                finding.get("detected_by", self.name),
                repo or None,
                owners,
            )
        except Exception as e:
            logger.error("Failed to insert sre_incidents row: %s", e)
            return False

        # 2. Best-effort: create a GitHub issue and link it back.
        if scm is not None and repo:
            title = f"[SRE-{severity.upper()}] {finding.get('title', 'Unknown Issue')}"
            body = self._format_issue_body(finding, app)
            labels = [
                "sre",
                f"severity:{severity}",
                f"category:{category}",
                "devai:sre-detected",
            ]
            try:
                if hasattr(scm, "create_issue_idempotent"):
                    issue = await scm.create_issue_idempotent(
                        repo,
                        title,
                        body,
                        labels,
                        dedupe_labels=["devai:sre-detected"],
                    )
                else:
                    issue = await scm.create_issue(repo, title, body, labels)
                issue_number = issue.get("number") or issue.get("iid")

                if owners and issue_number:
                    with contextlib.suppress(Exception):
                        await scm._request(
                            "POST",
                            f"/repos/{repo}/issues/{issue_number}/assignees",
                            json={"assignees": owners[:3]},
                        )

                if issue_number:
                    with contextlib.suppress(Exception):
                        await self.db.pool.execute(
                            "UPDATE sre_incidents SET scm_issue_number = $1 WHERE id = $2",
                            int(issue_number),
                            incident_id,
                        )
                    logger.info("Created SRE issue #%s on %s: %s", issue_number, repo, title)
            except Exception as e:
                logger.warning("SCM issue creation failed for %s (%s): %s", repo, severity, e)

        # 3. Remember it for de-duplication on the next scan.
        with contextlib.suppress(Exception):
            memory = AgentMemory(self.db.pool if hasattr(self.db, "pool") else self.db)
            await memory.remember(
                agent=self.name,
                content=f"Recorded incident {incident_id}: {finding.get('title', '')}",
                memory_type="episodic",
                repo=repo or "cluster",
                tags=[category, finding.get("title", "")[:30]],
            )

        return True

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

    @staticmethod
    def _resolve_repo(finding: dict[str, Any], app: dict[str, Any] | None) -> str:
        """Pick the best SCM repo for a finding.

        Priority: explicit ``finding.repo`` → app's configured ``repo`` →
        infer from the first ``ghcr.io/<org>/<name>`` image. We only infer
        from GHCR images because that maps cleanly onto a GitHub repo;
        gcr.io / docker.io paths do not.
        """
        explicit = finding.get("repo") or (app.get("repo") if app else "")
        if explicit:
            return explicit

        if not app:
            return ""

        for image in app.get("images", []) or []:
            if not image:
                continue
            ref = image.split("@", 1)[0].split(":", 1)[0]
            if ref.startswith("ghcr.io/"):
                parts = ref[len("ghcr.io/") :].split("/")
                if len(parts) >= 2:
                    return f"{parts[0]}/{parts[1]}"
        return ""

    def _format_issue_body(self, finding: dict, app: dict | None) -> str:
        severity = finding.get("severity", "medium").upper()
        category = finding.get("category", "unknown")
        detected_by = finding.get("detected_by", "sre_monitor")

        body = f"""## [{severity}] {finding.get("title", "")}

**Category:** {category}
**Detected by:** {detected_by}
**Affected:** {app.get("namespace", "unknown")}/{app.get("name", "unknown") if app else "unknown"}

### Evidence
```
{json.dumps(finding.get("evidence", finding), indent=2)[:2000]}
```

### Description
{finding.get("description", "No description available.")}

### Recommended Fix
{finding.get("recommendation", "Investigate the evidence above and apply appropriate fix.")}

---
*This issue was automatically created by the DevAI SRE monitoring system.*
"""
        return body
