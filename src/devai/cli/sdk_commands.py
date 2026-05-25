"""``devai sdk`` subcommands — quick CLI smoke-tests for the SDK.

Useful for proving a deployed registry is reachable and the catalog
has what you expect, without writing Python. Internally these all go
through :class:`devai.sdk.DevAI`, so success here means the SDK is
usable from any consuming app.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from devai.config import settings
from devai.sdk import DevAI

sdk_app = typer.Typer(
    name="sdk",
    help="Smoke-test the DevAI SDK against a live registry / API.",
    no_args_is_help=True,
)
console = Console()


def _client(
    registry_url: str = "", api_url: str = "", token: str = ""
) -> DevAI:
    return DevAI(
        registry_url=registry_url or settings.registry_url,
        registry_token=token or settings.registry_token,
        api_url=api_url,
    )


@sdk_app.command("health")
def health(
    registry_url: str = typer.Option("", "--registry-url"),
    token: str = typer.Option("", "--token"),
) -> None:
    """Probe the registry. Prints reachability + per-kind counts."""
    devai = _client(registry_url=registry_url, token=token)
    h = devai.health()
    console.print(h)
    if devai.registry:
        counts = devai.registry.counts()
        table = Table("kind", "count")
        for k, v in counts.items():
            table.add_row(k, str(v))
        console.print(table)


@sdk_app.command("skills")
def list_skills(
    category: str = typer.Option("", "--category"),
    registry_url: str = typer.Option("", "--registry-url"),
    token: str = typer.Option("", "--token"),
) -> None:
    devai = _client(registry_url=registry_url, token=token)
    items = devai.skills()
    if category:
        items = [s for s in items if s.category == category]
    table = Table("name", "category", "version", "title")
    for s in items:
        table.add_row(s.name, s.category, s.version, s.title)
    console.print(table)
    console.print(f"[dim]{len(items)} skill(s)[/]")


@sdk_app.command("agents")
def list_agents(
    registry_url: str = typer.Option("", "--registry-url"),
    token: str = typer.Option("", "--token"),
) -> None:
    devai = _client(registry_url=registry_url, token=token)
    items = devai.agents()
    table = Table("name", "version", "model", "framework")
    for a in items:
        table.add_row(
            a.name, a.version, f"{a.model_provider}/{a.model_name}", a.framework
        )
    console.print(table)
    console.print(f"[dim]{len(items)} agent(s)[/]")


@sdk_app.command("prompts")
def list_prompts(
    registry_url: str = typer.Option("", "--registry-url"),
    token: str = typer.Option("", "--token"),
) -> None:
    devai = _client(registry_url=registry_url, token=token)
    items = devai.prompts()
    table = Table("name", "version", "description")
    for p in items:
        table.add_row(p.name, p.version, p.description[:60])
    console.print(table)
    console.print(f"[dim]{len(items)} prompt(s)[/]")


@sdk_app.command("get")
def get(
    kind: str = typer.Argument(..., help="skill | prompt | mcp-server | agent"),
    name: str = typer.Argument(...),
    registry_url: str = typer.Option("", "--registry-url"),
    token: str = typer.Option("", "--token"),
) -> None:
    devai = _client(registry_url=registry_url, token=token)
    item = {
        "skill": devai.skill,
        "prompt": devai.prompt,
        "mcp-server": devai.mcp_server,
        "agent": devai.agent,
    }[kind](name)
    if item is None:
        console.print(f"[red]{kind} {name} not found[/]")
        raise typer.Exit(code=1)
    console.print(item.raw)
