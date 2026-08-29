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

from devai.identity import Principal, new_trace_id
from devai.services.redact import redact_secrets

logger = logging.getLogger(__name__)
router = APIRouter()


def _principal_from_webhook(provider: str, payload: dict[str, Any]) -> Principal:
    """Build a Principal from a GitHub/GitLab/ADO webhook payload.

    All three providers surface a ``sender`` block with a login. Email
    is rarely populated on private accounts, so we synthesize one — the
    important thing is that downstream audit code can answer "who pushed
    this?" with a stable handle.
    """
    sender = payload.get("sender") or payload.get("user") or {}
    login = (
        sender.get("login")
        or sender.get("username")
        or sender.get("name")
        or payload.get("uniqueName")  # ADO
        or "unknown"
    )
    email = sender.get("email") or ""
    return Principal.webhook(provider=provider, sender_login=login, sender_email=email)


def _provider_from_path(path: str) -> str:
    """Map a webhook URL path to its SCM provider name."""
    if "github" in path:
        return "github"
    if "gitlab" in path:
        return "gitlab"
    if "ado" in path:
        return "azure_devops"
    return "scm"


def _webhook_redis(request: Request) -> Any:
    """The shared Redis client (via the pipeline service's state manager), or
    None when the pipeline is disabled — webhook dedup degrades, never breaks."""
    svc = getattr(request.app.state, "pipeline_service", None)
    sm = getattr(svc, "state_manager", None) if svc is not None else None
    return getattr(sm, "redis", None)


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
        or request.headers.get("X-Gitlab-Token", "")  # GitLab
        or request.headers.get("X-Webhook-Secret", "")  # ADO
    )

    # Webhooks are exempt from the DEVAI_REQUIRE_AUTH gate (see devai.authz)
    # precisely because they authenticate via HMAC signature — so when that
    # auth posture is on, an unset webhook secret must FAIL CLOSED rather than
    # accept unsigned requests. In dev (require_auth off) we keep the lenient
    # behavior so local testing without a secret still works.
    if config.github_webhook_secret:
        if not scm.verify_webhook_signature(body, signature, config.github_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif getattr(config, "require_auth", False):
        logger.warning("Rejecting webhook: DEVAI_REQUIRE_AUTH is on but no webhook secret is configured")
        raise HTTPException(status_code=401, detail="Webhook signature verification not configured")

    # Idempotency: a webhook can be re-delivered (GitHub retries, manual redelivery)
    # and the issue labeled+opened pair are two deliveries for one issue. Drop a
    # delivery already accepted so it can't spawn a duplicate run. Best-effort —
    # no Redis (pipeline disabled) just skips the check.
    delivery_id = (
        request.headers.get("X-GitHub-Delivery")  # GitHub
        or request.headers.get("X-Gitlab-Event-UUID")  # GitLab
        or request.headers.get("Request-Id")  # ADO
        or ""
    )
    redis = _webhook_redis(request)
    if delivery_id and redis is not None:
        try:
            fresh = await redis.set(f"devai:webhook:delivery:{delivery_id}", "1", nx=True, ex=86400)
        except Exception:  # noqa: BLE001 — dedup must never break webhook intake
            fresh = True
        if not fresh:
            logger.info("webhook delivery %s already processed — ignoring duplicate", delivery_id)
            return {"status": "duplicate"}

    # Determine event type from headers (provider-specific)
    event_type = (
        request.headers.get("X-GitHub-Event", "")  # GitHub
        or request.headers.get("X-Gitlab-Event", "")  # GitLab
        or "ado_webhook"  # ADO
    )

    payload = await request.json()

    # Identity: stamp the SCM sender onto the trigger so downstream
    # agents know which human pushed the issue / opened the PR.
    provider = _provider_from_path(str(request.url.path))
    principal = _principal_from_webhook(provider, payload)
    trace_id = new_trace_id()

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
            await _trigger_from_normalized_event(request, normalized, principal, trace_id)
    else:
        # Fall back to legacy GitHub-specific routing for projects v2
        if event_type == "projects_v2_item":
            await _route_event(request, event_type, payload, principal, trace_id)

    return {"status": "accepted"}


async def _trigger_from_normalized_event(
    request: Request,
    event: dict[str, Any],
    principal: Principal,
    trace_id: str,
) -> None:
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

    # Guardrail: fence issue content as data before it reaches an agent.
    from devai.services.guardrails import sanitize_untrusted_text

    requirements = sanitize_untrusted_text(requirements, "issue")

    logger.info(
        "Pipeline triggered from %s issue #%s on %s",
        event.get("trigger_type", "unknown"),
        issue_number,
        repo,
    )

    # Post acknowledgement via SCM abstraction
    try:
        from devai.scm import create_scm_client

        config = request.app.state.config
        scm = create_scm_client(config)
        await scm.add_comment(
            repo,
            issue_number,
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
        _run_pipeline(
            request, repo, requirements, event.get("trigger_type", "scm"), str(issue_number), principal, trace_id
        )
    )


async def _route_event(
    request: Request,
    event_type: str,
    payload: dict[str, Any],
    principal: Principal,
    trace_id: str,
) -> None:
    """Route GitHub events to the LangGraph ALM pipeline."""
    config = request.app.state.config

    # --- 1. Issue labeled → trigger pipeline ---
    if event_type == "issues" and payload.get("action") == "labeled":
        label_name = payload.get("label", {}).get("name", "")

        # Trigger on the pipeline label OR on "requirement" label
        if label_name in (config.pipeline_label, "requirement", "devai:requirement"):
            await _trigger_from_issue(request, payload, principal, trace_id)
            return

    # --- 2. Issue opened with requirement label already on it ---
    if event_type == "issues" and payload.get("action") == "opened":
        labels = [lbl.get("name", "") for lbl in payload.get("issue", {}).get("labels", [])]
        if any(name in (config.pipeline_label, "requirement", "devai:requirement") for name in labels):
            await _trigger_from_issue(request, payload, principal, trace_id)
            return

    # --- 3. Issue comment with /devai command ---
    if event_type == "issue_comment" and payload.get("action") == "created":
        comment_body = payload.get("comment", {}).get("body", "").strip()
        if comment_body.startswith("/devai run") or comment_body.startswith("/devai build"):
            await _trigger_from_issue_comment(request, payload, principal, trace_id)
            return

    # --- 4. Projects v2 item moved to ready column ---
    if event_type == "projects_v2_item":
        action = payload.get("action", "")
        if action in ("edited", "created"):
            await _trigger_from_project_card(request, payload, principal, trace_id)
            return

    logger.debug("Ignoring event: %s/%s", event_type, payload.get("action", ""))


async def _trigger_from_issue(
    request: Request,
    payload: dict[str, Any],
    principal: Principal,
    trace_id: str,
) -> None:
    """Trigger the ALM pipeline from a GitHub issue."""
    issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    issue_title = issue.get("title", "")

    requirements = _build_requirements_from_issue(issue)

    logger.info(
        "Pipeline triggered from issue #%d on %s: %s (by %s)",
        issue_number,
        repo,
        issue_title,
        principal.email,
    )

    await _post_trigger_comment(request, repo, issue_number)

    asyncio.create_task(
        _run_pipeline(request, repo, requirements, "github_issue", str(issue_number), principal, trace_id)
    )


async def _trigger_from_issue_comment(
    request: Request,
    payload: dict[str, Any],
    principal: Principal,
    trace_id: str,
) -> None:
    """Trigger pipeline from a /devai command in an issue comment."""
    issue = payload["issue"]
    repo = payload["repository"]["full_name"]
    issue_number = issue["number"]
    comment_body = payload.get("comment", {}).get("body", "")

    parts = comment_body.split(maxsplit=2)
    override_reqs = parts[2] if len(parts) > 2 else ""

    if override_reqs:
        from devai.services.guardrails import sanitize_untrusted_text

        requirements = sanitize_untrusted_text(override_reqs, "comment")
    else:
        requirements = _build_requirements_from_issue(issue)

    logger.info("Pipeline triggered from comment on #%d on %s (by %s)", issue_number, repo, principal.email)

    await _post_trigger_comment(request, repo, issue_number)
    asyncio.create_task(
        _run_pipeline(request, repo, requirements, "github_issue", str(issue_number), principal, trace_id)
    )


async def _trigger_from_project_card(
    request: Request,
    payload: dict[str, Any],
    principal: Principal,
    trace_id: str,
) -> None:
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
            issue_number,
            repo,
        )

        await _post_trigger_comment(request, repo, issue_number)
        asyncio.create_task(
            _run_pipeline(request, repo, requirements, "project_card", str(issue_number), principal, trace_id)
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

    from devai.services.guardrails import sanitize_untrusted_text

    return sanitize_untrusted_text("\n".join(parts), "issue")


async def _post_trigger_comment(request: Request, repo: str, issue_number: int) -> None:
    """Post a comment confirming pipeline was triggered via SCM abstraction."""
    try:
        from devai.scm import create_scm_client

        config = request.app.state.config
        scm = create_scm_client(config)

        await scm.add_comment(
            repo,
            issue_number,
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
    principal: Principal,
    trace_id: str,
) -> None:
    """Run the ALM pipeline as a background task.

    Routing:
      - If `settings.pipeline_enabled` is True AND `app.state.pipeline_service`
        is started, dispatch through the Fiber-style blueprint runtime.
        Blueprint selection: `pr-review` for PR triggers, otherwise the
        configured default (`alm-pipeline`).
      - Otherwise fall back to the legacy LangGraph `ALMOrchestrator`.

    The fallback path stays intact so flipping `DEVAI_PIPELINE_ENABLED`
    is a one-line cut-over (and reversible).
    """
    from devai.scm import create_scm_client

    config = request.app.state.config
    state_manager = request.app.state.state_manager
    pipeline_service = getattr(request.app.state, "pipeline_service", None)

    # ── New path: blueprint runtime ──────────────────────────────────
    if getattr(config, "pipeline_enabled", False) and pipeline_service is not None:
        blueprint = _select_blueprint_for_trigger(config, trigger_type)
        try:
            task_id = await pipeline_service.dispatch(
                intent=requirements,
                blueprint=blueprint,
                repo=repo,
                trigger_type=trigger_type,
                label=f"{trigger_type}:{trigger_ref}"[:80],
                agent_context={"trigger_ref": trigger_ref, "requirements": requirements},
                principal=principal.to_dict(),
                trace_id=trace_id,
            )
            logger.info(
                "Dispatched pipeline task %s blueprint=%s repo=%s trigger=%s",
                task_id,
                blueprint,
                repo,
                trigger_type,
            )
            return
        except Exception:
            # Fall through to legacy path on dispatch failure so a misconfigured
            # blueprint doesn't kill a webhook delivery silently.
            logger.exception("Pipeline dispatch failed — falling back to legacy ALMOrchestrator")

    # ── Legacy path: LangGraph ALMOrchestrator ───────────────────────
    from devai.graph.orchestrator import ALMOrchestrator

    scm = create_scm_client(config)

    try:
        orchestrator = ALMOrchestrator(scm, state_manager, config)

        final_state = await orchestrator.run(
            repo_full_name=repo,
            requirements=requirements,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            principal=principal,
            trace_id=trace_id,
        )

        # Post completion comment on the originating issue
        if trigger_ref.isdigit():
            stage = final_state.get("stage", "unknown")
            status_icon = "white_check_mark" if stage in ("deployed", "done") else "x"

            timings = final_state.get("agent_timings", {})
            timing_lines = "\n".join(f"| {agent} | {dur:.1f}s |" for agent, dur in timings.items())

            tracking = final_state.get("supervisor_tracking_issue")
            tracking_ref = f"\n**Tracking Issue:** #{tracking}" if tracking else ""

            await scm.add_comment(
                repo,
                int(trigger_ref),
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
                    repo,
                    int(trigger_ref),
                    f"## Pipeline Failed\n\n:x: Error: `{redact_secrets(str(e))[:200]}`\n\nCheck the DevAI dashboard for details.",
                )

    finally:
        await scm.close()


def _select_blueprint_for_trigger(config, trigger_type: str) -> str:
    """Map a webhook trigger to the right blueprint.

    Falls back to the configured default when the trigger doesn't map to
    a known specialized blueprint.
    """
    tt = (trigger_type or "").lower()
    if tt in {"pull_request", "pr", "github_pr"}:
        return getattr(config, "pipeline_pr_review_blueprint", "pr-review")
    return getattr(config, "pipeline_default_blueprint", "alm-pipeline")
