"""``devai adk`` subcommands — Agent Development Kit CLI.

Scaffolds new catalog entries on disk and (optionally) publishes them
to a live aregistry. Mirrors ``devai sdk`` (which only reads the
catalog) so authoring and consuming surfaces stay symmetric.

Subcommands::

    devai adk new-skill <name> [--category=review] [--description=...]
    devai adk new-agent <name> [--skill=...] [--prompt=...]
    devai adk new-prompt <name>
    devai adk new-mcp-server <name> [--endpoint=...]
    devai adk publish <yaml-or-dir> [--registry-url=...]
    devai adk validate <yaml-or-dir>

The scaffolds write into ``architecture/registry-seeds/<kind>/`` by
default (overridable with ``--seeds-root``), matching the layout the
``devai-registry-bootstrap`` Job reads.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from devai.adk import (
    Agent,
    McpServer,
    Prompt,
    Publisher,
    Skill,
    scaffold_agent,
    scaffold_mcp_server,
    scaffold_prompt,
    scaffold_skill,
)
from devai.config import settings

adk_app = typer.Typer(
    name="adk",
    help="Author + publish skills / agents / prompts / MCP servers.",
    no_args_is_help=True,
)
console = Console()


@adk_app.command("new-skill")
def new_skill(
    name: str = typer.Argument(..., help="Skill name (kebab-case)"),
    description: str = typer.Option("", "--description", "-d"),
    category: str = typer.Option("review", "--category", "-c"),
    version: str = typer.Option("1", "--version"),
    seeds_root: Path = typer.Option(Path("architecture/registry-seeds"), "--seeds-root"),
) -> None:
    path = scaffold_skill(
        name,
        description=description,
        category=category,
        version=version,
        seeds_root=seeds_root,
    )
    console.print(f"[green]wrote[/] {path}")


@adk_app.command("new-prompt")
def new_prompt(
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
    content: str = typer.Option("", "--content"),
    version: str = typer.Option("1", "--version"),
    seeds_root: Path = typer.Option(Path("architecture/registry-seeds"), "--seeds-root"),
) -> None:
    path = scaffold_prompt(
        name,
        description=description,
        content=content,
        version=version,
        seeds_root=seeds_root,
    )
    console.print(f"[green]wrote[/] {path}")


@adk_app.command("new-mcp-server")
def new_mcp_server(
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
    endpoint: str = typer.Option("", "--endpoint"),
    version: str = typer.Option("1", "--version"),
    seeds_root: Path = typer.Option(Path("architecture/registry-seeds"), "--seeds-root"),
) -> None:
    path = scaffold_mcp_server(
        name,
        description=description,
        endpoint=endpoint,
        version=version,
        seeds_root=seeds_root,
    )
    console.print(f"[green]wrote[/] {path}")


@adk_app.command("new-agent")
def new_agent(
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
    skill: str = typer.Option("", "--skill"),
    prompt: str = typer.Option("", "--prompt"),
    model_provider: str = typer.Option("anthropic", "--model-provider"),
    model: str = typer.Option("claude-sonnet-4-20250514", "--model"),
    version: str = typer.Option("1", "--version"),
    seeds_root: Path = typer.Option(Path("architecture/registry-seeds"), "--seeds-root"),
) -> None:
    path = scaffold_agent(
        name,
        description=description,
        skill=skill,
        prompt=prompt,
        model_provider=model_provider,
        model=model,
        version=version,
        seeds_root=seeds_root,
    )
    console.print(f"[green]wrote[/] {path}")


@adk_app.command("validate")
def validate(target: Path = typer.Argument(..., help="YAML file or directory.")) -> None:
    """Parse + translate every file to the wire shape without POSTing."""
    files = list(_walk(target))
    if not files:
        console.print(f"[red]no YAML files under {target}[/]")
        raise typer.Exit(code=1)
    table = Table("kind", "name", "result")
    failures = 0
    for f in files:
        doc = yaml.safe_load(f.read_text()) or {}
        kind = doc.get("kind", "")
        name = doc.get("metadata", {}).get("name", "") or doc.get("spec", {}).get("name", "")
        try:
            _builder_for(doc).to_dict()
            table.add_row(kind, name, "[green]ok[/]")
        except Exception as e:  # noqa: BLE001
            table.add_row(kind, name, f"[red]{e}[/]")
            failures += 1
    console.print(table)
    if failures:
        raise typer.Exit(code=2)


@adk_app.command("publish")
def publish(
    target: Path = typer.Argument(..., help="YAML file or directory."),
    registry_url: str = typer.Option(
        "", "--registry-url", help="Default: settings.registry_url."
    ),
    token: str = typer.Option("", "--token"),
) -> None:
    url = registry_url or settings.registry_url
    if not url:
        console.print(
            "[red]no registry URL[/] — pass --registry-url or set DEVAI_REGISTRY_URL"
        )
        raise typer.Exit(code=1)
    files = list(_walk(target))
    if not files:
        console.print(f"[red]no YAML files under {target}[/]")
        raise typer.Exit(code=1)

    pub = Publisher(registry_url=url, token=token)
    table = Table("kind", "name", "status")
    for f in files:
        doc = yaml.safe_load(f.read_text()) or {}
        try:
            builder = _builder_for(doc)
        except Exception as e:  # noqa: BLE001
            table.add_row("?", str(f), f"[red]parse: {e}[/]")
            continue
        result = pub.publish(builder)
        color = "green" if result.ok else "red"
        table.add_row(
            result.kind,
            result.name,
            f"[{color}]{result.status}[/]" + (f" — {result.error}" if not result.ok else ""),
        )
    console.print(table)
    summary = pub.summary()
    if summary.failed:
        raise typer.Exit(code=2)


# ---- helpers ---------------------------------------------------------------


def _walk(target: Path):
    """Yield every YAML file under `target` (file or directory)."""
    if target.is_file():
        if target.suffix in (".yaml", ".yml"):
            yield target
        return
    if target.is_dir():
        yield from sorted(target.rglob("*.yaml"))
        yield from sorted(target.rglob("*.yml"))


def _builder_for(doc: dict):
    """Convert an on-disk YAML envelope into the matching ADK builder."""
    kind = doc.get("kind", "")
    spec = doc.get("spec") or {}
    name = doc.get("metadata", {}).get("name") or spec.get("name") or ""
    if kind == "Skill":
        b = Skill(name)
        if "description" in spec:
            b.description(spec["description"])
        if "category" in spec:
            b.category(spec["category"])
        if "displayName" in spec:
            b.title(spec["displayName"])
        if "version" in spec:
            b.version(spec["version"])
        return b
    if kind == "Prompt":
        b = Prompt(name)
        if "version" in spec:
            b.version(spec["version"])
        if "description" in spec:
            b.description(spec["description"])
        if "template" in spec or "content" in spec:
            b.content(spec.get("template") or spec.get("content", ""))
        return b
    if kind == "MCPServer":
        b = McpServer(name)
        if "description" in spec:
            b.description(spec["description"])
        if "version" in spec:
            b.version(spec["version"])
        if "endpoint" in spec:
            b.remote(spec["endpoint"])
        return b
    if kind == "Agent":
        b = Agent(name)
        if "description" in spec:
            b.description(spec["description"])
        if "version" in spec:
            b.version(spec["version"])
        if spec.get("skill"):
            b.skill(spec["skill"])
        if spec.get("promptRef"):
            b.prompt(spec["promptRef"])
        llm = spec.get("llm") or {}
        if llm.get("provider") and llm.get("model"):
            b.model(llm["provider"], llm["model"])
        return b
    raise ValueError(f"unknown kind: {kind!r}")
