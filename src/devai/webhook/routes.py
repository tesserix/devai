"""Webhook routes — triggers the LangGraph ALM pipeline from any SCM provider.

Supports:
  - GitHub (issues, comments, projects v2)
  - GitLab (issues, notes)
  - Azure DevOps (work items via service hooks)

The SCM abstraction layer normalizes events from any provider into a
common format, so the pipeline runs identically regardless of source.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook/github")
@router.post("/webhook/gitlab")
@router.post("/webhook/ado")
@router.post("/webhook/scm")
async def scm_webhook(request: Request) -> dict[str, str]:
    """Handle incoming webhook events from any SCM provider (GitHub, GitLab, ADO)."""
    body = await request.body()
    config = request.app.state.config

    from devai.scm import create_scm_client

    scm = create_scm_client(config)

    # Verify signature based on provider
    signature = (
        request.headers.get("X-Hub-Signature-256", "")  # GitHub
        or request.headers.get("X-Gitlab-Token", "")     # GitLab
        or request.headers.get("X-Webhook-Secret", "")    # ADO
    )

    if config.github_webhook_secret and not scm.verify_webhook_signature(
        body, signature, config.github_webhook_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Determine event type from headers (provider-specific)
    event_type = (
        request.headers.get("X-GitHub-Event", "")         # GitHub
        or request.headers.get("X-Gitlab-Event", "")       # GitLab
        or "ado_webhook"                                    # ADO
    )

    payload = await request.json()

    # Use SCM abstraction to normalize the event
    normalized = scm.parse_webhook_event(event_type, payload)
    await scm.close()

    if normalized:
        # Check if the event should trigger the pipeline
        labels = normalized.get("labels", [])
        command = normalized.get("command", "")

        should_trigger = (
            config.pipeline_label in labels
            or "requirement" in labels
            or "devai:requirement" in labels
            or command.startswith("/devai")
        )

        if should_trigger:
            await _trigger_from_normalized_event(request, normalized)
    else:
        # Fall back to legacy GitHub-specific routing for projects v2
        if event_type == "projects_v2_item":
            await _route_event(request, event_type, payload)

    return {"status": "accepted"}


async def _trigger_from_normalized_event(request: Request, event: dict[str, Any]) -> None:
    """Trigger the pipeline from a normalized SCM event."""
    repo = event["repo"]
    issue_number = event["issue_number"]
    title = event.get("title", "")
    body_text = event.get("body", "")
    labels = event.get("labels", [])

    requirements = (
        f"# Requirement: Issue #{issue_number} — {title}\n"
        f"**Labels:** {', '.join(labels)}\n\n"
        f"## Description\n\n{body_text}"
    )

    # Guardrail: sanitize requirements before feeding to agents
    try:
        from devai.services.guardrails import InputSanitizer

        sanitizer = InputSanitizer()
        requirements, warnings = sanitizer.sanitize_requirements(requirements)
        for w in warnings:
            logger.warning("Webhook guardrail: %s", w)
    except Exception:
        pass

    logger.info(
        "Pipeline triggered from %s issue #%s on %s",
        event.get("trigger_type", "unknown"), issue_number, repo,
    )

    # Post acknowledgement via SCM abstraction
    try:
        from devai.scm import create_scm_client
        config = request.app.state.config
        scm = create_scm_client(config)
        await scm.add_comment(
            repo, issue_number,
            "**DevAI Pipeline Triggered**\n\n"
            "The ALM pipeline has started processing this requirement.\n\n"
            "The Supervisor Agent will analyze this request, create a tracking issue "
            "with the architecture plan, and coordinate specialist agents.\n\n"
            "Progress updates will be posted as each stage completes.",
        )
        await scm.close()
    except Exception as e:
        logger.warning("Failed to post trigger comment: %s", e)

    asyncio.create_task(
        _run_pipeline(request, repo, requirements, event.get("trigger_type", "scm"), str(issue_number))
    )


async def _route_event(request: Request, event_type: str, payload: dict[str, Any]) -> None:
    """Route GitHub events to the LangGraph ALM pipeline."""
    config = request.app.state.config

    # --- 1. Issue labeled → trigger pipeline ---
    if event_type == "issues" and payload.get("action") == "labeled":
        label_name = payload.get("label", {}).get("name", "")

        # Trigger on the pipeline label OR on "requirement" label
        if label_name in (config.pipeline_label, "requirement", "devai:requirement"):
            await _trigger_from_issue(request, payload)
            return

    # --- 2. Issue opened with requirement label already on it ---
    if event_type == "issues" and payload.get("action") == "opened":
        labels = [lbl.get("name", "") for lbl in payload.get("issue", {}).get("labels", [])]
        if any(name in (config.pipeline_label, "requirement", "devai:requirement") for name in labels):
            await _trigger_from_issue(request, payload)
            return

    # --- 3. Issue comment with /devai command ---
    if event_type == "issue_comment" and payload.get("action") == "created":
        comment_body = payload.get("comment", {}).get("body", "").strip()
        if comment_body.startswith("/devai run") or comment_body.startswith("/devai build"):
            await _trigger_from_issue_comment(request, payload)
            return

    # --- 4. Projects v2 item moved to ready column ---
    if event_type == "projects_v2_item":
        action = payload.get("action", "")
        if action in ("edited", "created"):
            await _trigger_from_project_card(request, payload)
            return

    logger.debug("Ignoring event: %s/%s", event_type, payload.get("action", ""))


async def _trigger_from_issue(request: Request, payload: dict[str, Any]) -> None:
    """Trigger the ALM pipeline from a GitHub issue."""
    issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    issue_title = issue.get("title", "")

    requirements = _build_requirements_from_issue(issue)

    logger.info(
        "Pipeline triggered from issue #%d on %s: %s",
        issue_number, repo, issue_title,
    )

    await _post_trigger_comment(request, repo, issue_number)

    asyncio.create_task(
        _run_pipeline(request, repo, requirements, "github_issue", str(issue_number))
    )


async def _trigger_from_issue_comment(request: Request, payload: dict[str, Any]) -> None:
    """Trigger pipeline from a /devai command in an issue comment."""
    issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    comment_body = payload.get("comment", {}).get("body", "")

    parts = comment_body.split(maxsplit=2)
    override_reqs = parts[2] if len(parts) > 2 else ""

    requirements = override_reqs or _build_requirements_from_issue(issue)

    logger.info("Pipeline triggered from comment on #%d on %s", issue_number, repo)

    await _post_trigger_comment(request, repo, issue_number)
    asyncio.create_task(
        _run_pipeline(request, repo, requirements, "github_issue", str(issue_number))
    )


async def _trigger_from_project_card(request: Request, payload: dict[str, Any]) -> None:
    """Trigger pipeline when a project card is moved to the ready column."""
    config = request.app.state.config
    changes = payload.get("changes", {})

    field_value = changes.get("field_value", {})
    new_value = field_value.get("to", {})

    if isinstance(new_value, dict):
        column_name = new_value.get("name", "")
    elif isinstance(new_value, str):
        column_name = new_value
    else:
        return

    if column_name != config.project_ready_column:
        logger.debug("Project card moved to '%s' — not the ready column", column_name)
        return

    item = payload.get("projects_v2_item", {})
    content_type = item.get("content_type", "")
    content_node_id = item.get("content_node_id", "")

    if content_type != "Issue" or not content_node_id:
        logger.debug("Project card is not an issue, skipping")
        return

    try:
        from devai.scm import create_scm_client

        scm = create_scm_client(config)

        issue_data = await _resolve_project_item_issue(scm, content_node_id)

        if not issue_data:
            logger.warning("Could not resolve project item %s to an issue", content_node_id)
            await scm.close()
            return

        repo = issue_data["repo"]
        issue_number = issue_data["number"]

        issue = await scm.get_issue(repo, issue_number)
        await scm.close()

        requirements = _build_requirements_from_issue(issue)

        logger.info(
            "Pipeline triggered from project card → issue #%d on %s",
            issue_number, repo,
        )

        await _post_trigger_comment(request, repo, issue_number)
        asyncio.create_task(
            _run_pipeline(request, repo, requirements, "project_card", str(issue_number))
        )

    except Exception as e:
        logger.error("Failed to process project card event: %s", e)


# --- Helpers ---

def _build_requirements_from_issue(issue: dict[str, Any]) -> str:
    """Build a comprehensive requirements string from an issue/work item."""
    number = issue.get("number", "?")
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
    milestone = issue.get("milestone", {})
    milestone_title = milestone.get("title", "") if milestone else ""
    assignees = [a.get("login", "") for a in issue.get("assignees", [])]

    parts = [f"# Requirement: Issue #{number} — {title}\n"]

    if labels:
        parts.append(f"**Labels:** {', '.join(labels)}")

    if milestone_title:
        parts.append(f"**Milestone:** {milestone_title}")

    if assignees:
        parts.append(f"**Assignees:** {', '.join(assignees)}")

    parts.append(f"\n## Description\n\n{body}")

    return "\n".join(parts)


async def _post_trigger_comment(request: Request, repo: str, issue_number: int) -> None:
    """Post a comment confirming pipeline was triggered via SCM abstraction."""
    try:
        from devai.scm import create_scm_client

        config = request.app.state.config
        scm = create_scm_client(config)

        await scm.add_comment(
            repo, issue_number,
            "**DevAI Pipeline Triggered**\n\n"
            "The ALM pipeline has started processing this requirement.\n\n"
            "Stages: Supervisor → Orchestrator → Document Analysis → "
            "Tech Detection → Requirements → Epic → Stories → Plan → "
            "Code → Review → Security → Build → Test → Deploy\n\n"
            "Progress updates will be posted on the tracking issue.",
        )
        await scm.close()
    except Exception as e:
        logger.warning("Failed to post trigger comment: %s", e)


async def _resolve_project_item_issue(
    scm: Any,
    node_id: str,
) -> dict[str, Any] | None:
    """Resolve a GitHub Projects v2 content_node_id to an issue.

    Uses the GitHub GraphQL API. Only works with GitHub SCM client.
    """
    query = """
    query($id: ID!) {
        node(id: $id) {
            ... on Issue {
                number
                title
                repository {
                    nameWithOwner
                }
            }
        }
    }
    """
    try:
        # This uses the GitHub-specific _request method
        resp = await scm._request(
            "POST",
            "https://api.github.com/graphql",
            json={"query": query, "variables": {"id": node_id}},
        )
        data = resp.json()
        node = data.get("data", {}).get("node", {})
        if node and node.get("number"):
            return {
                "number": node["number"],
                "title": node.get("title", ""),
                "repo": node.get("repository", {}).get("nameWithOwner", ""),
            }
    except Exception as e:
        logger.error("GraphQL resolution failed for %s: %s", node_id, e)

    return None


async def _run_pipeline(
    request: Request,
    repo: str,
    requirements: str,
    trigger_type: str,
    trigger_ref: str,
) -> None:
    """Run the LangGraph ALM pipeline as a background task."""
    from devai.graph.orchestrator import ALMOrchestrator
    from devai.scm import create_scm_client

    config = request.app.state.config
    state_manager = request.app.state.state_manager

    scm = create_scm_client(config)

    try:
        orchestrator = ALMOrchestrator(scm, state_manager, config)

        final_state = await orchestrator.run(
            repo_full_name=repo,
            requirements=requirements,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
        )

        # Post completion comment on the originating issue
        if trigger_ref.isdigit():
            stage = final_state.get("stage", "unknown")
            status_icon = "white_check_mark" if stage in ("deployed", "done") else "x"

            timings = final_state.get("agent_timings", {})
            timing_lines = "\n".join(
                f"| {agent} | {dur:.1f}s |"
                for agent, dur in timings.items()
            )

            tracking = final_state.get("supervisor_tracking_issue")
            tracking_ref = f"\n**Tracking Issue:** #{tracking}" if tracking else ""

            await scm.add_comment(
                repo, int(trigger_ref),
                f"## Pipeline Complete\n\n"
                f":{status_icon}: **Stage:** {stage}\n{tracking_ref}\n\n"
                f"| Agent | Duration |\n|---|---|\n{timing_lines}\n\n"
                f"**Run ID:** `{final_state.get('run_id', 'unknown')}`",
            )

    except Exception as e:
        logger.exception("Pipeline failed for %s: %s", repo, e)

        # Post failure comment
        if trigger_ref.isdigit():
            with contextlib.suppress(Exception):
                await scm.add_comment(
                    repo, int(trigger_ref),
                    f"## Pipeline Failed\n\n"
                    f":x: Error: `{str(e)[:200]}`\n\n"
                    f"Check the DevAI dashboard for details.",
                )

    finally:
        await scm.close()
