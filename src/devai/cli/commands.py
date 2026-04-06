"""DevAI CLI — command-line interface for triggering and monitoring pipelines."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from devai.config import settings

app = typer.Typer(
    name="devai",
    help="AI-powered development lifecycle automation (LangGraph + LangSmith)",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", "-r", help="Target repository (org/repo)"),
    requirements: str = typer.Option(None, "--requirements", "-m", help="Requirements text"),
    from_issue: int = typer.Option(None, "--from-issue", "-i", help="GitHub issue number to use as input"),
) -> None:
    """Trigger a new DevAI ALM pipeline run (LangGraph)."""
    if not requirements and not from_issue:
        console.print("[red]Error:[/red] Provide --requirements or --from-issue")
        raise typer.Exit(1)

    asyncio.run(_trigger_pipeline(repo, requirements, from_issue))


async def _trigger_pipeline(repo: str, requirements: str | None, from_issue: int | None) -> None:
    from devai.core.github_client import GitHubClient
    from devai.core.state import StateManager
    from devai.graph.orchestrator import ALMOrchestrator
    from devai.services.tracing import init_langsmith

    # Initialize LangSmith tracing
    settings.export_langsmith_env()
    init_langsmith()

    state = StateManager(settings.redis_url)
    github = GitHubClient(settings)

    try:
        # If from-issue, fetch the issue body as requirements
        trigger_type = "cli"
        trigger_ref = "cli"
        if from_issue and not requirements:
            issue = await github.get_issue(repo, from_issue)
            requirements = f"Issue #{from_issue}: {issue['title']}\n\n{issue['body']}"
            trigger_type = "github_issue"
            trigger_ref = str(from_issue)

        # Progress callback for CLI output
        def on_progress(step: str, status: str, detail: str) -> None:
            icon = {"running": "[yellow]...[/yellow]", "completed": "[green]OK[/green]"}.get(status, status)
            console.print(f"  {icon} {step}: {detail}")

        # Run the LangGraph pipeline
        orchestrator = ALMOrchestrator(github, state, settings)

        console.print(Panel(
            f"[bold]DevAI ALM Pipeline[/bold]\n"
            f"Repo: {repo}\n"
            f"Mode: LangGraph + LangSmith",
            title="Starting Pipeline",
            border_style="green",
        ))

        final_state = await orchestrator.run(
            repo_full_name=repo,
            requirements=requirements or "",
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            on_progress=on_progress,
        )

        # Display results
        console.print("\n")
        _display_results(final_state)

    finally:
        await state.close()
        await github.close()


def _display_results(state: dict) -> None:
    """Display pipeline results in a rich table."""
    console.print(Panel(
        f"[bold]Run ID:[/bold] {state.get('run_id', 'unknown')}\n"
        f"[bold]Stage:[/bold] {state.get('stage', 'unknown')}\n"
        f"[bold]Build:[/bold] {state.get('build_status', 'n/a')}\n"
        f"[bold]Deploy:[/bold] {state.get('deploy_status', 'n/a')}",
        title="Pipeline Complete",
        border_style="green" if state.get("stage") == "deployed" else "red",
    ))

    # Agent timings
    timings = state.get("agent_timings", {})
    if timings:
        table = Table(title="Agent Timings")
        table.add_column("Agent", style="cyan")
        table.add_column("Duration", style="yellow")

        for agent, duration in timings.items():
            table.add_row(agent, f"{duration:.1f}s")
        console.print(table)

    # A2A Messages
    messages = state.get("a2a_messages", [])
    if messages:
        table = Table(title=f"A2A Messages ({len(messages)} total)")
        table.add_column("From", style="cyan")
        table.add_column("To", style="green")
        table.add_column("Type", style="yellow")
        table.add_column("Subject")

        for msg in messages[-15:]:  # Show last 15
            table.add_row(
                msg.get("from_agent", ""),
                msg.get("to_agent", ""),
                msg.get("message_type", ""),
                msg.get("subject", "")[:50],
            )
        console.print(table)


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
            run_data = await state.get_run(run_id)
            if not run_data:
                console.print(f"[red]Run {run_id} not found[/red]")
                return

            console.print(f"\n[bold]Pipeline Run: {run_id}[/bold]")
            console.print(f"  Stage:   {run_data.get('stage', 'unknown')}")
            console.print(f"  Repo:    {run_data.get('repo', 'unknown')}")
            console.print(f"  Created: {run_data.get('created_at', 'unknown')}")

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
                        "waiting_approval": "[blue]waiting_approval[/blue]",
                    }.get(info["status"], info["status"])
                    table.add_row(name, status_style, info.get("error", "") or "")
                console.print(table)
        else:
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
    """Run the webhook server with LangGraph pipeline."""
    asyncio.run(_serve(host, port))


async def _serve(host: str, port: int) -> None:
    import uvicorn

    from devai.services.tracing import init_langsmith

    # Initialize LangSmith tracing
    settings.export_langsmith_env()
    init_langsmith()

    from devai.core.github_client import GitHubClient
    from devai.core.state import StateManager
    from devai.webhook.app import create_app

    state = StateManager(settings.redis_url, settings.redis_result_ttl, settings.redis_lock_ttl)
    GitHubClient(settings)

    # Create the webhook app with LangGraph orchestrator
    from devai.core.event_bus import EventBus

    event_bus = EventBus(
        stream_name=settings.nats_stream,
        max_deliver=settings.nats_max_deliver,
        ack_wait=settings.nats_ack_wait,
    )
    await event_bus.connect(settings.nats_url)

    webhook_app = create_app(event_bus, state, settings)

    console.print(Panel(
        f"[bold]DevAI Server[/bold]\n"
        f"Host: {host}:{port}\n"
        f"Pipeline: LangGraph\n"
        f"Tracing: LangSmith",
        title="Server Starting",
        border_style="green",
    ))

    config = uvicorn.Config(webhook_app, host=host, port=port, log_level=settings.log_level)
    server = uvicorn.Server(config)
    await server.serve()


@app.command()
def agents() -> None:
    """List all DevAI ALM agents and their configuration."""
    table = Table(title="DevAI ALM Agents (LangGraph Pipeline)")
    table.add_column("Agent", style="cyan")
    table.add_column("AI Provider")
    table.add_column("Role in ALM")
    table.add_column("A2A Connections")

    agent_info = [
        ("Document Analyzer", "Groq (Llama 3.3)", "Ingest PDFs, URLs, specs", "-> Requirements Analyst"),
        ("Tech Detector", "Groq (Llama 3.3)", "Auto-detect tech stack", "-> All downstream agents"),
        ("Requirements Analyst", "Groq (Llama 3.3)", "Analyze & refine requirements", "-> Product Director, EM"),
        ("Product Director", "OpenAI (o3)", "Create Epics & User Stories", "-> Engineering Manager"),
        ("Engineering Manager", "Claude (Sonnet 4)", "Technical planning", "-> Senior Developer, QA"),
        ("Senior Developer", "Claude (Sonnet 4)", "Implement + compile/lint/test", "-> DB Engineer"),
        ("DB Engineer", "Claude (Sonnet 4)", "Schema migrations (Liquibase)", "-> Staff Reviewer"),
        ("Staff Reviewer", "OpenAI Codex (Sandbox)", "Code review", "-> Security Expert / Sr Dev"),
        ("Security Expert", "Claude (Sonnet 4)", "SAST/SCA/Secrets/OWASP scan", "-> CI Monitor / Sr Dev"),
        ("CI Monitor", "Groq (Llama 3.3)", "Monitor GitHub Actions builds", "-> QA Tester / Sr Dev"),
        ("QA Tester", "Claude (Sonnet 4)", "Write & run E2E tests", "-> Infra Provisioner"),
        ("Infra Provisioner", "Claude (Sonnet 4)", "Helm charts / VM deploy scripts", "-> Release Manager"),
        ("Release Manager", "Groq (Llama 3.3)", "Merge PR & deploy to prod", "-> All (broadcast)"),
    ]

    for name, provider, role, a2a in agent_info:
        table.add_row(name, provider, role, a2a)

    console.print(table)

    # Show pipeline flow
    console.print("\n[bold]Pipeline Flow (LangGraph):[/bold]")
    console.print(
        "  Document Analyzer -> Tech Detector -> Requirements Analyst\n"
        "  -> Product Director (Epic) -> Product Director (Stories)\n"
        "  -> Engineering Manager -> Senior Developer (compile+lint+test)\n"
        "  -> DB Engineer (migrations) -> Staff Reviewer\n"
        "  -> [if approved] Security Expert (SAST/SCA/Secrets/OWASP)\n"
        "  -> [if pass] CI Monitor -> QA Tester\n"
        "  -> Infra Provisioner -> Release Manager\n"
        "  -> [if changes/block] -> Senior Developer (loop, max 3)"
    )

    console.print("\n[bold]Guardrails:[/bold]")
    console.print(
        "  - Security Expert: SAST + SCA + Secret Detection + OWASP + Container\n"
        "  - Approval Gates: configurable per-agent human approval\n"
        "  - Memory: cross-run learning (episodic/semantic/procedural)\n"
        "  - Checkpointing: resume from last successful stage\n"
        "  - Circuit Breakers: API failure isolation\n"
        "  - Timeouts: 15-min per agent, 3-min per API call"
    )
