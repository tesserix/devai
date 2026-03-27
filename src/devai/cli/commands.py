"""DevAI CLI — command-line interface for triggering and monitoring pipelines."""

from __future__ import annotations

import asyncio
import sys

import typer
from rich.console import Console
from rich.table import Table

from devai.config import settings

app = typer.Typer(
    name="devai",
    help="AI-powered development lifecycle automation",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", "-r", help="Target repository (org/repo)"),
    requirements: str = typer.Option(None, "--requirements", "-m", help="Requirements text"),
    from_issue: int = typer.Option(None, "--from-issue", "-i", help="GitHub issue number to use as input"),
) -> None:
    """Trigger a new DevAI pipeline run."""
    if not requirements and not from_issue:
        console.print("[red]Error:[/red] Provide --requirements or --from-issue")
        raise typer.Exit(1)

    asyncio.run(_trigger_pipeline(repo, requirements, from_issue))


async def _trigger_pipeline(repo: str, requirements: str | None, from_issue: int | None) -> None:
    from devai.core.event_bus import EventBus
    from devai.core.github_client import GitHubClient
    from devai.core.pipeline import PipelineOrchestrator
    from devai.core.state import StateManager
    from devai.models import TriggerType

    event_bus = EventBus(
        stream_name=settings.nats_stream,
        max_deliver=settings.nats_max_deliver,
        ack_wait=settings.nats_ack_wait,
    )
    state = StateManager(settings.redis_url)
    github = GitHubClient(settings)

    try:
        await event_bus.connect(settings.nats_url)

        # If from-issue, fetch the issue body as requirements
        if from_issue and not requirements:
            issue = await github.get_issue(repo, from_issue)
            requirements = f"Issue #{from_issue}: {issue['title']}\n\n{issue['body']}"
            trigger_type = TriggerType.GITHUB_ISSUE
            trigger_ref = str(from_issue)
        else:
            trigger_type = TriggerType.CLI
            trigger_ref = "cli"

        orchestrator = PipelineOrchestrator(event_bus, state, settings)
        ctx = await orchestrator.trigger(
            repo_full_name=repo,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            requirements=requirements or "",
        )

        console.print(f"\n[green]Pipeline triggered successfully![/green]")
        console.print(f"  Run ID:  [bold]{ctx.run_id}[/bold]")
        console.print(f"  Repo:    {ctx.repo_full_name}")
        console.print(f"  Stage:   {ctx.stage.value}")
        console.print(f"\nTrack progress: [cyan]devai status {ctx.run_id}[/cyan]")

    finally:
        await event_bus.close()
        await state.close()
        await github.close()


@app.command()
def status(
    run_id: str = typer.Argument(None, help="Pipeline run ID"),
    repo: str = typer.Option(None, "--repo", "-r", help="Filter by repository"),
    last: int = typer.Option(5, "--last", "-n", help="Number of recent runs to show"),
) -> None:
    """Check pipeline run status."""
    asyncio.run(_show_status(run_id, repo, last))


async def _show_status(run_id: str | None, repo: str | None, last: int) -> None:
    from devai.core.state import StateManager

    state = StateManager(settings.redis_url)

    try:
        if run_id:
            # Show detailed status for a specific run
            run_data = await state.get_run(run_id)
            if not run_data:
                console.print(f"[red]Run {run_id} not found[/red]")
                return

            console.print(f"\n[bold]Pipeline Run: {run_id}[/bold]")
            console.print(f"  Stage:   {run_data.get('stage', 'unknown')}")
            console.print(f"  Repo:    {run_data.get('repo', 'unknown')}")
            console.print(f"  Created: {run_data.get('created_at', 'unknown')}")

            # Agent statuses
            agents = await state.get_agent_statuses(run_id)
            if agents:
                console.print("\n[bold]Agent Status:[/bold]")
                table = Table()
                table.add_column("Agent", style="cyan")
                table.add_column("Status")
                table.add_column("Error", style="red")

                for name, info in agents.items():
                    status_style = {
                        "completed": "[green]completed[/green]",
                        "running": "[yellow]running[/yellow]",
                        "failed": "[red]failed[/red]",
                    }.get(info["status"], info["status"])
                    table.add_row(name, status_style, info.get("error", "") or "")
                console.print(table)
        else:
            # List recent runs
            if repo:
                run_ids = await state.list_runs_by_repo(repo, last)
            else:
                run_ids = await state.list_runs(last)

            if not run_ids:
                console.print("[yellow]No pipeline runs found[/yellow]")
                return

            table = Table(title="Recent Pipeline Runs")
            table.add_column("Run ID", style="cyan")
            table.add_column("Repo")
            table.add_column("Stage")
            table.add_column("Created")

            for rid in run_ids:
                run_data = await state.get_run(rid)
                if run_data:
                    table.add_row(
                        rid,
                        run_data.get("repo", ""),
                        run_data.get("stage", ""),
                        run_data.get("created_at", ""),
                    )
            console.print(table)
    finally:
        await state.close()


@app.command()
def serve(
    host: str = typer.Option(settings.host, "--host", help="Server host"),
    port: int = typer.Option(settings.port, "--port", "-p", help="Server port"),
) -> None:
    """Run the webhook server and all agent workers."""
    asyncio.run(_serve(host, port))


async def _serve(host: str, port: int) -> None:
    import uvicorn

    from devai.core.event_bus import EventBus
    from devai.core.github_client import GitHubClient
    from devai.core.state import StateManager
    from devai.agents.product_director import ProductDirectorAgent
    from devai.agents.engineering_manager import EngineeringManagerAgent
    from devai.agents.senior_developer import SeniorDeveloperAgent
    from devai.agents.staff_reviewer import StaffReviewerAgent
    from devai.agents.qa_tester import QATesterAgent

    # Initialize shared resources
    event_bus = EventBus(
        stream_name=settings.nats_stream,
        max_deliver=settings.nats_max_deliver,
        ack_wait=settings.nats_ack_wait,
    )
    state = StateManager(settings.redis_url, settings.redis_result_ttl, settings.redis_lock_ttl)
    github = GitHubClient(settings)

    await event_bus.connect(settings.nats_url)

    # Start all agents as background tasks
    agents = [
        ProductDirectorAgent(event_bus, state, github, settings),
        EngineeringManagerAgent(event_bus, state, github, settings),
        SeniorDeveloperAgent(event_bus, state, github, settings),
        StaffReviewerAgent(event_bus, state, github, settings),
        QATesterAgent(event_bus, state, github, settings),
    ]

    for agent in agents:
        asyncio.create_task(agent.start())

    console.print(f"[green]DevAI agents started ({len(agents)} agents)[/green]")

    # Inject shared resources into the webhook app
    from devai.webhook.app import create_app

    webhook_app = create_app(event_bus, state, settings)

    # Start the webhook server
    config = uvicorn.Config(webhook_app, host=host, port=port, log_level=settings.log_level)
    server = uvicorn.Server(config)
    console.print(f"[green]Webhook server starting on {host}:{port}[/green]")
    await server.serve()


@app.command()
def agents() -> None:
    """List all DevAI agents and their configuration."""
    from devai.models import AgentRole

    table = Table(title="DevAI Agents")
    table.add_column("Agent", style="cyan")
    table.add_column("AI Provider")
    table.add_column("Subscribe Subject")
    table.add_column("Publish Subject")

    agent_info = [
        ("Product Director", "OpenAI Codex (Responses API)", "devai.pipeline.trigger", "devai.pipeline.stories_ready"),
        ("Engineering Manager", "Claude (Messages API + Tools)", "devai.pipeline.stories_ready", "devai.pipeline.plan_ready"),
        ("Senior Developer", "Claude (Messages API + Tools)", "devai.pipeline.plan_ready", "devai.pipeline.code_ready"),
        ("Staff Reviewer", "OpenAI Codex (Sandbox)", "devai.pipeline.code_ready", "devai.pipeline.review_complete"),
        ("QA Tester", "Claude (Messages API + Tools)", "devai.pipeline.review_complete", "devai.pipeline.tests_complete"),
    ]

    for name, provider, sub, pub in agent_info:
        table.add_row(name, provider, sub, pub)

    console.print(table)
