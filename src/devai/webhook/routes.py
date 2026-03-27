"""GitHub webhook routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from devai.core.pipeline import PipelineOrchestrator
from devai.models import TriggerType
from devai.webhook.signature import verify_github_signature

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook/github")
async def github_webhook(request: Request) -> dict[str, str]:
    """Handle incoming GitHub webhook events."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    config = request.app.state.config

    if not verify_github_signature(body, signature, config.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()

    await _route_event(request, event_type, payload)
    return {"status": "accepted"}


async def _route_event(request: Request, event_type: str, payload: dict) -> None:
    """Route GitHub events to pipeline triggers."""
    config = request.app.state.config
    event_bus = request.app.state.event_bus
    state = request.app.state.state_manager

    orchestrator = PipelineOrchestrator(event_bus, state, config)

    if event_type == "issues" and payload.get("action") == "labeled":
        label_name = payload.get("label", {}).get("name", "")
        if label_name == config.pipeline_label:
            issue = payload["issue"]
            repo = payload["repository"]["full_name"]
            await orchestrator.trigger(
                repo_full_name=repo,
                trigger_type=TriggerType.GITHUB_ISSUE,
                trigger_ref=str(issue["number"]),
                requirements=f"Issue #{issue['number']}: {issue['title']}\n\n{issue.get('body', '')}",
            )
            logger.info("Pipeline triggered from issue #%d on %s", issue["number"], repo)

    elif event_type == "projects_v2_item":
        # GitHub Projects v2 events require GraphQL to resolve column
        # For now, log and handle in a future iteration
        logger.info("Received projects_v2_item event: %s", payload.get("action"))

    else:
        logger.debug("Ignoring event: %s/%s", event_type, payload.get("action", ""))
