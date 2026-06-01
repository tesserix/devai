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

# `devai pipeline ...` — Fiber-style blueprint runtime (new). Coexists with
# the legacy `devai run ...` command which drives the LangGraph orchestrator
# directly. Once the blueprint runtime reaches parity we'll deprecate `run`.
pipeline_app = typer.Typer(
    name="pipeline",
    help="Run / list blueprints via the Fiber-style stage runtime.",
    no_args_is_help=True,
)
app.add_typer(pipeline_app, name="pipeline")

# `devai specializations ...` — YAML catalog of agent role personas
specializations_app = typer.Typer(
    name="specializations",
    help="List / show / validate YAML specialization (skill) definitions.",
    no_args_is_help=True,
)
app.add_typer(specializations_app, name="specializations")

# `devai sdk ...` — quick smoke-tests for the consuming-side SDK.
# `devai adk ...` — author + publish new catalog entries.
from devai.cli.adk_commands import adk_app  # noqa: E402 — registered after the base app
from devai.cli.sdk_commands import sdk_app  # noqa: E402 — registered after the base app

app.add_typer(sdk_app, name="sdk")
app.add_typer(adk_app, name="adk")


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", "-r", help="Target repository (org/repo)"),
    requirements: str = typer.Option(None, "--requirements", "-m", help="Requirements text"),
    from_issue: int = typer.Option(None, "--from-issue", "-i", help="GitHub issue number to use as input"),
) -> None:
    """Trigger a new DevAI ALM pipeline run (durable blueprint runtime)."""
    if not requirements and not from_issue:
        console.print("[red]Error:[/red] Provide --requirements or --from-issue")
        raise typer.Exit(1)

    asyncio.run(_trigger_pipeline(repo, requirements, from_issue))


async def _trigger_pipeline(repo: str, requirements: str | None, from_issue: int | None) -> None:
    from devai.core.state import StateManager
    from devai.pipeline.bootstrap import build_runtime
    from devai.pipeline.pipeline import Pipeline
    from devai.pipeline.types import DevAITask
    from devai.scm import create_scm_client
    from devai.services.tracing import init_langsmith

    # Initialize LangSmith tracing
    settings.export_langsmith_env()
    init_langsmith()

    state = StateManager(settings.redis_url)
    scm = None
    try:
        scm = create_scm_client(settings)
    except Exception:  # noqa: BLE001
        console.print("[yellow]warning:[/yellow] SCM client unavailable — stages run without SCM")

    bundle = None
    try:
        # If from-issue, fetch the issue body as requirements
        trigger_type = "cli"
        trigger_ref = "cli"
        if from_issue and not requirements and scm is not None:
            issue = await scm.get_issue(repo, from_issue)
            requirements = f"Issue #{from_issue}: {issue['title']}\n\n{issue.get('body', '')}"
            trigger_type = "github_issue"
            trigger_ref = str(from_issue)

        # Build the same StageDeps + registry the API/worker use, then run the
        # blueprint inline through the durable pipeline (Temporal when
        # DEVAI_WORKFLOW_PROVIDER=temporal, else in-process).
        bundle = await build_runtime(settings, scm=scm, state_manager=state)
        pipeline = Pipeline(
            bundle.deps,
            registry=bundle.registry,
            blueprint_dir=getattr(settings, "pipeline_blueprint_dir", "blueprints"),
            default_stage_timeout=float(getattr(settings, "pipeline_default_stage_timeout", 900)),
        )
        pipeline.load_blueprints()

        def on_event(task, event) -> None:  # noqa: ANN001
            icon = {
                "started": "[yellow]...[/yellow]",
                "completed": "[green]OK[/green]",
                "failed": "[red]x[/red]",
                "skipped": "[dim]skip[/dim]",
            }.get(event.phase.value, event.phase.value)
            console.print(f"  {icon} {event.stage}: {event.message or event.error or ''}")

        pipeline.add_event_callback(on_event)

        blueprint = getattr(settings, "pipeline_default_blueprint", "alm-pipeline")
        task = DevAITask(
            intent=requirements or "",
            blueprint=blueprint,
            repo=repo,
            trigger_type=trigger_type,
            agent_context={"requirements": requirements or "", "trigger_ref": trigger_ref},
        )

        console.print(
            Panel(
                f"[bold]DevAI ALM Pipeline[/bold]\nRepo: {repo}\nBlueprint: {blueprint}\n"
                f"Mode: durable pipeline ({getattr(settings, 'workflow_provider', 'inproc')})",
                title="Starting Pipeline",
                border_style="green",
            )
        )

        result = await pipeline.run_once(task)

        # Display results
        console.print("\n")
        _display_results(result.to_dict())

    finally:
        if bundle is not None:
            await bundle.aclose()
        await state.close()
        if scm is not None:
            await scm.close()


def _display_results(state: dict) -> None:
    """Display pipeline results in a rich table (DevAITask.to_dict() shape)."""
    task_state = state.get("state", "unknown")
    ok = task_state == "completed"
    console.print(
        Panel(
            f"[bold]Run ID:[/bold] {state.get('id', 'unknown')}\n"
            f"[bold]State:[/bold] {task_state}\n"
            f"[bold]Stages:[/bold] {len(state.get('stages_completed', []))} done, "
            f"{len(state.get('stages_failed', []))} failed\n"
            f"[bold]PR:[/bold] {state.get('pr_number') or 'n/a'}   "
            f"[bold]Branch:[/bold] {state.get('branch_name') or 'n/a'}"
            + (f"\n[bold red]Error:[/bold red] {state.get('error')}" if state.get("error") else ""),
            title="Pipeline Complete",
            border_style="green" if ok else "red",
        )
    )

    # Per-stage timings from the stage-event timeline.
    events = [e for e in state.get("stage_events", []) if e.get("phase") == "completed"]
    if events:
        table = Table(title="Stage Timings")
        table.add_column("Stage", style="cyan")
        table.add_column("Duration", style="yellow")
        for e in events:
            table.add_row(e.get("stage", ""), f"{e.get('duration_ms', 0) / 1000:.1f}s")
        console.print(table)

    # A2A Messages (when an agent recorded them onto the handover bag)
    messages = (state.get("agent_context") or {}).get("a2a_messages", [])
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

    # Two pub/sub surfaces:
    # 1. Legacy `EventBus` — used by the LangGraph PipelineOrchestrator
    #    and the /readyz probe for backward compat.
    # 2. New `EventBusAdapter` — used by the Fiber-style PipelineService
    #    and `devai start-agent` legacy subscribers. Both point at the
    #    same NATS connection but the adapter layer is the supported one.
    from devai.adapters.event_bus import create_and_connect_event_bus
    from devai.core.event_bus import EventBus

    event_bus = EventBus(
        stream_name=settings.nats_stream,
        max_deliver=settings.nats_max_deliver,
        ack_wait=settings.nats_ack_wait,
    )
    try:
        await event_bus.connect(settings.nats_url)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] legacy EventBus connect failed: {e}")

    event_bus_adapter = await create_and_connect_event_bus(settings)

    webhook_app = create_app(event_bus, state, settings, event_bus_adapter=event_bus_adapter)

    console.print(
        Panel(
            f"[bold]DevAI Server[/bold]\nHost: {host}:{port}\nPipeline: LangGraph\nTracing: LangSmith",
            title="Server Starting",
            border_style="green",
        )
    )

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

    # Subject map — useful when wiring NATS subscribers from outside DevAI
    subject_table = Table(title="ALM Agent NATS Subjects (legacy subscriber mode)")
    subject_table.add_column("Agent", style="cyan")
    subject_table.add_column("Subscribes to")
    subject_table.add_column("Publishes to")
    for agent_name, sub, pub in _agent_subject_map():
        subject_table.add_row(agent_name, sub or "—", pub or "—")
    console.print("\n")
    console.print(subject_table)

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


# ──────────────────────────────────────────────────────────────────────
# `devai pipeline ...` — Fiber-style blueprint runtime
# ──────────────────────────────────────────────────────────────────────


@pipeline_app.command("list")
def pipeline_list(
    blueprint_dir: str = typer.Option("blueprints", "--dir", "-d", help="Blueprint directory"),
) -> None:
    """List every blueprint discovered under `blueprints/`."""
    from devai.blueprint import discover_blueprints

    blueprints = discover_blueprints(blueprint_dir)
    if not blueprints:
        console.print(f"[yellow]No blueprints found under {blueprint_dir}[/yellow]")
        return

    table = Table(title=f"Blueprints ({len(blueprints)})")
    table.add_column("Name", style="cyan")
    table.add_column("Stages", justify="right")
    table.add_column("Description")
    for name, bp in sorted(blueprints.items()):
        table.add_row(name, str(len(bp.stages)), (bp.description or "").split("\n")[0][:80])
    console.print(table)


@pipeline_app.command("stages")
def pipeline_stages() -> None:
    """List every stage key registered with the default StageRegistry."""
    from devai.blueprint.registry import StageRegistry, register_defaults

    reg = StageRegistry()
    register_defaults(reg)
    table = Table(title=f"Registered stages ({len(reg.known_stages())})")
    table.add_column("Stage key", style="cyan")
    for key in reg.known_stages():
        table.add_row(key)
    console.print(table)


@pipeline_app.command("validate")
def pipeline_validate(
    blueprint_dir: str = typer.Option("blueprints", "--dir", "-d", help="Blueprint directory"),
) -> None:
    """Parse every blueprint and confirm all stage references resolve."""
    from devai.blueprint import discover_blueprints
    from devai.blueprint.registry import StageRegistry, register_defaults

    reg = StageRegistry()
    register_defaults(reg)
    blueprints = discover_blueprints(blueprint_dir)
    bad = 0
    for name, bp in sorted(blueprints.items()):
        missing = [s.stage for s in bp.stages if not reg.has(s.stage)]
        if missing:
            console.print(f"[red]✗[/red] {name}: missing stages {missing}")
            bad += 1
        else:
            console.print(f"[green]✓[/green] {name} ({len(bp.stages)} stages)")
    if bad:
        raise typer.Exit(1)


@pipeline_app.command("run")
def pipeline_run(
    blueprint: str = typer.Option(..., "--blueprint", "-b", help="Blueprint name (e.g. alm-pipeline)"),
    intent: str = typer.Option(..., "--intent", "-i", help="Free-form intent / requirements"),
    repo: str = typer.Option("", "--repo", "-r", help="Target repo (org/repo). Optional for review-only blueprints."),
    blueprint_dir: str = typer.Option("blueprints", "--dir", "-d", help="Blueprint directory"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Skip SCM + StateManager wiring. Stages run against None deps and short-circuit.",
    ),
) -> None:
    """Dispatch a single task through a blueprint and stream stage events."""
    asyncio.run(_pipeline_run_async(blueprint, intent, repo, blueprint_dir, dry_run))


async def _pipeline_run_async(
    blueprint_name: str,
    intent: str,
    repo: str,
    blueprint_dir: str,
    dry_run: bool,
) -> None:
    from devai.pipeline import DevAITask, Pipeline, StageDeps

    deps = StageDeps(config=settings)
    if not dry_run:
        try:
            from devai.core.state import StateManager
            from devai.scm import create_scm_client

            state_manager = StateManager(settings.redis_url)
            scm = create_scm_client(settings)
            deps = StageDeps(
                config=settings,
                scm=scm,
                state_manager=state_manager,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]Wiring real deps failed ({e}); falling back to dry-run.[/yellow]")

    pipeline = Pipeline(deps, blueprint_dir=blueprint_dir)
    pipeline.load_blueprints()

    def on_event(task, event) -> None:
        symbol = {
            "started": "→",
            "completed": "[green]✓[/green]",
            "failed": "[red]✗[/red]",
            "skipped": "[dim]↷[/dim]",
        }.get(event.phase.value, "·")
        suffix = f" ({event.duration_ms:.0f}ms)" if event.duration_ms else ""
        message = f" — {event.message}" if event.message else ""
        err = f" [red]{event.error}[/red]" if event.error else ""
        console.print(f"  {symbol} {event.stage}{suffix}{message}{err}")

    pipeline.add_event_callback(on_event)

    task = DevAITask(intent=intent, blueprint=blueprint_name, repo=repo)
    console.print(
        Panel(
            f"[bold]Blueprint:[/bold] {blueprint_name}\n"
            f"[bold]Task ID:[/bold] {task.id}\n"
            f"[bold]Repo:[/bold] {repo or '(none)'}\n"
            f"[bold]Mode:[/bold] {'dry-run' if dry_run else 'live'}",
            title="DevAI Pipeline Run",
            border_style="cyan",
        )
    )

    await pipeline.run_once(task)

    border = "green" if task.state.value == "completed" else "red"
    console.print(
        Panel(
            f"[bold]State:[/bold] {task.state.value}\n"
            f"[bold]Stages completed:[/bold] {len(task.stages_completed)}\n"
            f"[bold]Stages failed:[/bold] {len(task.stages_failed)}\n"
            f"[bold]Error:[/bold] {task.error or '(none)'}",
            title="Pipeline Finished",
            border_style=border,
        )
    )


# ──────────────────────────────────────────────────────────────────────
# `devai specializations ...` — YAML role catalog
# ──────────────────────────────────────────────────────────────────────


@specializations_app.command("list")
def specializations_list(
    category: str = typer.Option("", "--category", "-c", help="Filter by category."),
    directory: str = typer.Option("specializations", "--dir", "-d", help="Catalog directory."),
) -> None:
    """List every specialization in the catalog."""
    from devai.specializations import discover_specializations

    specs = discover_specializations(directory)
    if not specs:
        console.print(f"[yellow]No specializations found under {directory}[/yellow]")
        return
    if category:
        specs = {n: s for n, s in specs.items() if s.category == category}

    table = Table(title=f"Specializations ({len(specs)})")
    table.add_column("Name", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Provider")
    table.add_column("Risk")
    table.add_column("Bridge")
    table.add_column("Description")
    for name in sorted(specs):
        spec = specs[name]
        bridge = "legacy" if spec.legacy_python_class else "yaml-only"
        desc = (spec.description or "").replace("\n", " ").strip()[:60]
        table.add_row(name, spec.category, spec.llm_provider.value, spec.risk_level.value, bridge, desc)
    console.print(table)


@specializations_app.command("show")
def specializations_show(
    name: str = typer.Argument(..., help="Specialization name (e.g. senior_developer)."),
    directory: str = typer.Option("specializations", "--dir", "-d", help="Catalog directory."),
) -> None:
    """Print one specialization's full record (prompt, handover schema, etc.)."""
    from devai.specializations import discover_specializations

    specs = discover_specializations(directory)
    if name not in specs:
        console.print(f"[red]No specialization named {name!r}.[/red] Known: {', '.join(sorted(specs))}")
        raise typer.Exit(1)
    spec = specs[name]
    console.print(
        Panel(
            f"[bold]name:[/bold] {spec.name}\n"
            f"[bold]display_name:[/bold] {spec.display_name}\n"
            f"[bold]category:[/bold] {spec.category}\n"
            f"[bold]llm_provider:[/bold] {spec.llm_provider.value}\n"
            f"[bold]llm_model:[/bold] {spec.llm_model or '(provider default)'}\n"
            f"[bold]risk_level:[/bold] {spec.risk_level.value}\n"
            f"[bold]max_turns:[/bold] {spec.max_turns}\n"
            f"[bold]timeout:[/bold] {spec.timeout_seconds}s\n"
            f"[bold]output_key:[/bold] {spec.output_key}\n"
            f"[bold]legacy_python_class:[/bold] {spec.legacy_python_class or '(yaml-only)'}",
            title=f"Specialization: {spec.name}",
            border_style="cyan",
        )
    )

    if spec.description:
        console.print(f"\n[bold]Description:[/bold]\n{spec.description}\n")

    if spec.context_keys:
        console.print(f"[bold]Reads from agent_context:[/bold] {', '.join(spec.context_keys)}")

    if spec.allowed_tools:
        console.print(f"[bold]Allowed tools:[/bold] {', '.join(spec.allowed_tools)}")

    if spec.handover_schema:
        table = Table(title="Handover schema")
        table.add_column("Field")
        table.add_column("Type")
        table.add_column("Required")
        table.add_column("Description")
        for fname, fld in spec.handover_schema.items():
            table.add_row(fname, fld.type, "yes" if fld.required else "no", fld.description[:60])
        console.print(table)

    console.print(Panel(spec.system_prompt or "(empty)", title="system_prompt", border_style="dim"))


@specializations_app.command("validate")
def specializations_validate(
    directory: str = typer.Option("specializations", "--dir", "-d", help="Catalog directory."),
) -> None:
    """Parse every specialization YAML and surface load errors."""
    from devai.specializations import discover_specializations

    try:
        specs = discover_specializations(directory)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Catalog load failed:[/red] {e}")
        raise typer.Exit(1) from e

    console.print(f"[green]All {len(specs)} specializations parse cleanly.[/green]")
    counts: dict[str, int] = {}
    yaml_only = 0
    for spec in specs.values():
        counts[spec.category] = counts.get(spec.category, 0) + 1
        if not spec.legacy_python_class:
            yaml_only += 1
    for cat, n in sorted(counts.items()):
        console.print(f"  {cat:14} {n}")
    console.print(f"  [dim]yaml-only specs: {yaml_only}[/dim]")


# ──────────────────────────────────────────────────────────────────────
# `devai start-agent <name>` — run a single legacy agent as a NATS subscriber
# ──────────────────────────────────────────────────────────────────────


def _agent_class_registry() -> dict[str, type]:
    """Map agent name -> class for the legacy NATS-driven subscribers.

    Imports are lazy so a broken agent module doesn't prevent the CLI
    from listing the others.
    """
    from devai.agents.ci_monitor import CIMonitorAgent
    from devai.agents.db_engineer import DBEngineerAgent
    from devai.agents.document_analyzer import DocumentAnalyzerAgent
    from devai.agents.engineering_manager import EngineeringManagerAgent
    from devai.agents.infra_provisioner import InfraProvisionerAgent
    from devai.agents.product_director import ProductDirectorAgent
    from devai.agents.qa_tester import QATesterAgent
    from devai.agents.release_manager import ReleaseManagerAgent
    from devai.agents.requirements_analyst import RequirementsAnalystAgent
    from devai.agents.security_expert import SecurityExpertAgent
    from devai.agents.senior_developer import SeniorDeveloperAgent
    from devai.agents.staff_reviewer import StaffReviewerAgent
    from devai.agents.tech_detector import TechDetectorAgent

    return {
        "document_analyzer": DocumentAnalyzerAgent,
        "tech_detector": TechDetectorAgent,
        "requirements_analyst": RequirementsAnalystAgent,
        "product_director": ProductDirectorAgent,
        "engineering_manager": EngineeringManagerAgent,
        "senior_developer": SeniorDeveloperAgent,
        "db_engineer": DBEngineerAgent,
        "staff_reviewer": StaffReviewerAgent,
        "security_expert": SecurityExpertAgent,
        "ci_monitor": CIMonitorAgent,
        "qa_tester": QATesterAgent,
        "infra_provisioner": InfraProvisionerAgent,
        "release_manager": ReleaseManagerAgent,
    }


def _agent_subject_map() -> list[tuple[str, str, str]]:
    """Return (agent_name, subscribe_subject, publish_subject) for each agent."""
    out: list[tuple[str, str, str]] = []
    for name, klass in _agent_class_registry().items():
        sub = getattr(klass, "subscribe_subject", "") or ""
        pub = getattr(klass, "publish_subject", "") or ""
        out.append((name, sub, pub))
    return out


@app.command("start-agent")
def start_agent(
    name: str = typer.Argument(..., help="Agent name (e.g. senior_developer). Use 'list' to see all."),
) -> None:
    """Run a single ALM agent as a standalone NATS JetStream subscriber.

    The agent subscribes to its configured `subscribe_subject` with a
    durable consumer named `devai-<agent>`, processes each message via
    `_handle_message`, and publishes the result to `publish_subject` on
    success or to `devai.pipeline.errors` on failure.

    Run multiple in parallel (one per process) to scale the legacy
    pipeline horizontally. The new Fiber-style PipelineService publishes
    the same `devai.pipeline.*` subjects, so a mix is fine.
    """
    registry = _agent_class_registry()

    if name == "list":
        table = Table(title="Known agents (use `devai start-agent <name>`)")
        table.add_column("Name", style="cyan")
        table.add_column("Class")
        table.add_column("Subscribes to")
        table.add_column("Publishes to")
        for n, klass in registry.items():
            table.add_row(
                n,
                klass.__name__,
                getattr(klass, "subscribe_subject", "") or "—",
                getattr(klass, "publish_subject", "") or "—",
            )
        console.print(table)
        return

    if name not in registry:
        console.print(f"[red]Unknown agent {name!r}.[/red] Known: {', '.join(sorted(registry))}")
        raise typer.Exit(1)

    asyncio.run(_start_agent_async(name, registry[name]))


async def _start_agent_async(name: str, klass: type) -> None:
    from devai.adapters.event_bus import create_and_connect_event_bus
    from devai.core.state import StateManager
    from devai.scm import create_scm_client

    state = StateManager(settings.redis_url, settings.redis_result_ttl, settings.redis_lock_ttl)
    try:
        scm = create_scm_client(settings)
    except Exception as e:
        console.print(f"[red]SCM client construction failed:[/red] {e}")
        raise typer.Exit(1) from e

    adapter = await create_and_connect_event_bus(settings)

    agent = klass(scm, state, settings, adapter)
    try:
        await agent.start()
    except Exception as e:
        console.print(f"[red]Agent {name} failed to subscribe:[/red] {e}")
        await adapter.close()
        await state.close()
        raise typer.Exit(1) from e

    console.print(
        Panel(
            f"[bold]Agent:[/bold] {name}\n"
            f"[bold]Subscribes to:[/bold] {agent.subscribe_subject}\n"
            f"[bold]Publishes to:[/bold] {agent.publish_subject or '(none)'}\n"
            f"[bold]Durable:[/bold] devai-{name}\n"
            f"[bold]Adapter:[/bold] {adapter.provider_name}",
            title="DevAI Agent Worker",
            border_style="green",
        )
    )
    console.print("[dim]Listening — Ctrl-C to stop.[/dim]")

    stop = asyncio.Event()
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await state.close()
        except Exception:  # noqa: BLE001
            pass
