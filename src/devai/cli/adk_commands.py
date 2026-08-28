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

import json
import os
import time
import uuid
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from devai.adk import (
    AdkError,
    Agent,
    Dataset,
    EvalSuite,
    McpServer,
    Prompt,
    Publisher,
    SandboxClient,
    Skill,
    scaffold_agent,
    scaffold_mcp_server,
    scaffold_prompt,
    scaffold_skill,
)
from devai.config import settings
from devai.services.redact import scrub_structure

adk_app = typer.Typer(
    name="adk",
    help="Author + publish skills / agents / prompts / MCP servers.",
    no_args_is_help=True,
)
sandbox_app = typer.Typer(name="sandbox", help="Create, invoke, inspect, and destroy agent sandboxes.")
registry_app = typer.Typer(name="registry", help="Search and import immutable Registry agents into DevAI.")
adk_app.add_typer(sandbox_app, name="sandbox")
adk_app.add_typer(registry_app, name="registry")
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
    model: str = typer.Option("claude-sonnet-5", "--model"),
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
def validate(
    target: Path = typer.Argument(..., help="YAML file or directory."),
    deep: bool = typer.Option(False, "--deep", help="Resolve every local artifact reference."),
) -> None:
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
    if deep:
        from devai.adk.validation import validate_artifacts

        roots = [Path("architecture/registry-seeds")]
        for failure in validate_artifacts(files, deep=True, catalog_roots=roots):
            table.add_row("ref", failure.reference, f"[red]{failure.message}[/]")
            failures += 1
    console.print(table)
    if failures:
        raise typer.Exit(code=2)


@adk_app.command("publish")
def publish(
    target: Path = typer.Argument(..., help="YAML file or directory."),
    registry_url: str = typer.Option("", "--registry-url", help="Default: settings.registry_url."),
    token: str = typer.Option("", "--token"),
    api_url: str = typer.Option("", "--api-url", help="DevAI API used for gated agent publication."),
    session_cookie: str = typer.Option("", "--session-cookie"),
    api_token: str = typer.Option("", "--api-token"),
    eval_run_id: str = typer.Option("", "--eval-run-id"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    override_reason: str = typer.Option("", "--override-reason"),
) -> None:
    files = list(_walk(target))
    if not files:
        console.print(f"[red]no YAML files under {target}[/]")
        raise typer.Exit(code=1)

    url = registry_url or settings.registry_url
    pub: Publisher | None = None
    api_client: SandboxClient | None = None
    failures = 0
    table = Table("kind", "name", "status")
    try:
        for f in files:
            doc = yaml.safe_load(f.read_text()) or {}
            try:
                builder = _builder_for(doc)
            except Exception as e:  # noqa: BLE001
                table.add_row("?", str(f), f"[red]parse: {e}[/]")
                failures += 1
                continue
            if doc.get("kind") == "Agent":
                if api_client is None:
                    api_client = _new_sandbox_client(
                        api_url=api_url,
                        session_cookie=session_cookie,
                        token=api_token,
                    )
                manifest = deepcopy(doc)
                metadata = _mapping(manifest.get("metadata"))
                manifest["metadata"] = metadata
                annotations = _mapping(metadata.get("annotations"))
                metadata["annotations"] = annotations
                if eval_run_id:
                    annotations["devai.tesserix.app/eval-run-id"] = eval_run_id
                try:
                    response = api_client.publish_agent(
                        manifest,
                        overwrite=overwrite,
                        override_reason=override_reason,
                    )
                    gate = _mapping(response.get("gate"))
                    status = str(gate.get("status") or "published")
                    table.add_row("agent", builder.name, f"[green]{status}[/]")
                except AdkError as error:
                    table.add_row("agent", builder.name, f"[red]failed — {error}[/]")
                    failures += 1
                continue
            if not url:
                table.add_row(
                    str(doc.get("kind") or "?"),
                    builder.name,
                    "[red]failed — pass --registry-url or set DEVAI_REGISTRY_URL[/]",
                )
                failures += 1
                continue
            if pub is None:
                pub = Publisher(registry_url=url, token=token)
            result = pub.publish(builder)
            color = "green" if result.ok else "red"
            table.add_row(
                result.kind,
                result.name,
                f"[{color}]{result.status}[/]" + (f" — {result.error}" if not result.ok else ""),
            )
    finally:
        if api_client is not None:
            api_client.close()
    console.print(table)
    if failures or (pub is not None and pub.summary().failed):
        raise typer.Exit(code=2)


def _new_sandbox_client(*, api_url: str = "", session_cookie: str = "", token: str = "") -> SandboxClient:
    base_url = api_url or os.environ.get("DEVAI_API_URL", "") or settings.dashboard_base_url or "http://localhost:8080"
    cookie = session_cookie or os.environ.get("DEVAI_SESSION_COOKIE", "")
    bearer = token or os.environ.get("DEVAI_API_TOKEN", "")
    return SandboxClient(base_url=base_url, session_cookie=cookie, token=bearer)


@registry_app.command("search")
def registry_search(
    query: str = typer.Argument(..., help="Capability or behavior to find."),
    kind: list[str] = typer.Option(["Agent"], "--kind", help="Repeat to include more artifact kinds."),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """Search the Registry semantic index through DevAI's authorization boundary."""
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            result = client.search_registry(query, kinds=kind, limit=limit)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(result))
        return
    table = Table("rank", "kind", "name", "version", "description")
    for hit in result.get("hits") or []:
        table.add_row(
            str(hit.get("rank") or ""),
            str(hit.get("kind") or ""),
            str(hit.get("name") or ""),
            str(hit.get("version") or ""),
            str(hit.get("description") or ""),
        )
    console.print(table)


@registry_app.command("import")
def registry_import(
    registry_ref: str = typer.Argument(..., help="Exact registry://...@version Agent reference."),
    project_id: str = typer.Option(..., "--project"),
    idempotency_key: str = typer.Option("", "--idempotency-key"),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """Verify and lock one signed Registry Agent for a DevAI project."""
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            imported = client.import_agent(
                project_id=project_id,
                registry_ref=registry_ref,
                idempotency_key=idempotency_key or f"devai-cli-{uuid.uuid4()}",
            )
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(imported))
        return
    agent = imported.get("agent") or {}
    console.print(
        f"[green]{imported.get('state', 'ready')}[/] {imported.get('id', '')} · "
        f"{agent.get('name', '')}@{agent.get('version', '')}"
    )


@registry_app.command("imports")
def registry_imports(
    project_id: str = typer.Option(..., "--project"),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """List immutable Agent locks visible to the current tenant."""
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            imports = client.list_agent_imports(project_id)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(imports))
        return
    table = Table("id", "agent", "version", "conformance", "state")
    for imported in imports:
        agent = imported.get("agent") or {}
        conformance = imported.get("conformance") or {}
        table.add_row(
            str(imported.get("id") or ""),
            str(agent.get("name") or ""),
            str(agent.get("version") or ""),
            str(conformance.get("level") or ""),
            str(imported.get("state") or ""),
        )
    console.print(table)


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a YAML object")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _sandbox_spec_from_agent(
    path: Path,
    *,
    llm_connector: str,
    confirmed: bool,
    agent_version: str = "",
) -> dict[str, Any]:
    document = _load_yaml(path)
    if document.get("kind") != "Agent":
        raise ValueError(f"{path} is not an Agent artifact")
    spec = _mapping(document.get("spec"))
    metadata = _mapping(document.get("metadata"))
    name = str(metadata.get("name") or spec.get("name") or "")
    version = agent_version or str(spec.get("version") or metadata.get("tag") or "")
    llm = _mapping(spec.get("llm"))
    provider = str(llm.get("provider") or spec.get("modelProvider") or "")
    model = str(llm.get("model") or spec.get("modelName") or "")
    if not all((name, version, provider, model)):
        raise ValueError("agent needs a name, version, model provider, and model")

    defaults = _mapping(spec.get("sandbox"))
    result: dict[str, Any] = {
        **defaults,
        "agent": {"name": name, "version": version},
        "model": {"provider": provider, "model": model},
        "draft": document,
        "credentials": {"llm_connector": llm_connector, "confirmed": confirmed},
    }
    prompt_ref = str(spec.get("promptRef") or "")
    if prompt_ref:
        result["prompt"] = {"ref": prompt_ref, "version": "1"}
    return result


@sandbox_app.command("create")
def sandbox_create(
    agent: Path = typer.Argument(..., help="Unpublished Agent YAML to sandbox."),
    agent_version: str = typer.Option("", "--agent-version"),
    suite: Path | None = typer.Option(None, "--suite"),
    tool_mode: str = typer.Option("", "--tool-mode"),
    llm_connector: str = typer.Option("", "--llm-connector"),
    confirm_llm_connector: bool = typer.Option(False, "--confirm-llm-connector"),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """Create a sandbox from the exact draft artifact that will be published."""
    if bool(llm_connector) != confirm_llm_connector:
        console.print("[red]an LLM connector and explicit confirmation are required together[/]")
        raise typer.Exit(code=2)
    try:
        body = _sandbox_spec_from_agent(
            agent,
            llm_connector=llm_connector,
            confirmed=confirm_llm_connector,
            agent_version=agent_version,
        )
        if suite is not None:
            _, dataset_name, dataset_version, _ = _suite_settings(suite, None)
            body["dataset"] = {"ref": dataset_name, "version": dataset_version}
        if tool_mode:
            if tool_mode not in {"mock", "replay", "block", "real"}:
                raise ValueError("--tool-mode must be mock, replay, block, or real")
            body["tools"] = {"default_mode": tool_mode, "overrides": {}}
        client = _new_sandbox_client(api_url=api_url)
        try:
            created = client.create(body)
        finally:
            client.close()
    except (AdkError, OSError, ValueError) as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(created))
    else:
        console.print(f"[green]created[/] {created.get('id', '')} ({created.get('status', '')})")


@sandbox_app.command("from-import")
def sandbox_from_import(
    import_id: str = typer.Argument(..., help="Ready Agent import UUID."),
    provider: str = typer.Option("custom", "--provider"),
    model: str = typer.Option("portable-runtime", "--model"),
    tool_mode: str = typer.Option("mock", "--tool-mode"),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """Create a sandbox from a server-verified immutable import lock."""
    if tool_mode not in {"mock", "replay", "block", "real"}:
        console.print("[red]--tool-mode must be mock, replay, block, or real[/]")
        raise typer.Exit(code=2)
    spec = {
        "import_id": import_id,
        "model": {"provider": provider, "model": model},
        "tools": {"default_mode": tool_mode, "overrides": {}},
    }
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            created = client.create(spec)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(created))
    else:
        console.print(f"[green]created[/] {created.get('id', '')} ({created.get('status', '')})")


@sandbox_app.command("invoke")
def sandbox_invoke(
    sandbox_id: str = typer.Argument(...),
    message: str = typer.Argument(...),
    api_url: str = typer.Option("", "--api-url"),
) -> None:
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            invocation = client.invoke(sandbox_id, message)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    console.print(str(invocation.get("final_text") or ""))
    console.print(f"[dim]trace {invocation.get('id', '')}[/]")


@sandbox_app.command("wait")
def sandbox_wait(
    sandbox_id: str = typer.Argument(...),
    timeout: float = typer.Option(300.0, "--timeout", min=0.1),
    interval: float = typer.Option(2.0, "--interval", min=0.0),
    api_url: str = typer.Option("", "--api-url"),
) -> None:
    """Wait for a sandbox to become ready, fail, or exceed a bounded timeout."""
    deadline = time.monotonic() + timeout
    client = _new_sandbox_client(api_url=api_url)
    try:
        while True:
            record = client.get(sandbox_id)
            status = str(record.get("status") or "")
            if status == "ready":
                console.print(f"[green]ready[/] {sandbox_id}")
                return
            if status in {"failed", "destroyed"}:
                console.print(f"[red]{status}[/] {sandbox_id}")
                raise typer.Exit(code=2)
            if time.monotonic() >= deadline:
                console.print(f"[red]timed out waiting for {sandbox_id}[/]")
                raise typer.Exit(code=2)
            time.sleep(interval)
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    finally:
        client.close()


@sandbox_app.command("traces")
def sandbox_traces(
    sandbox_id: str = typer.Argument(...),
    api_url: str = typer.Option("", "--api-url"),
) -> None:
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            traces = client.traces(sandbox_id)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    console.print_json(json.dumps(traces))


@sandbox_app.command("destroy")
def sandbox_destroy(
    sandbox_id: str = typer.Argument(...),
    api_url: str = typer.Option("", "--api-url"),
) -> None:
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            client.destroy(sandbox_id)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    console.print(f"[green]destroyed[/] {sandbox_id}")


def _dataset_cases(
    path: Path,
    *,
    expected_name: str = "",
    expected_version: str = "",
) -> list[dict[str, Any]]:
    document = _load_yaml(path)
    if document.get("kind") != "Dataset":
        raise ValueError(f"{path} is not a Dataset artifact")
    spec = _mapping(document.get("spec"))
    metadata = _mapping(document.get("metadata"))
    name = str(metadata.get("name") or spec.get("name") or "")
    version = str(spec.get("version") or "")
    if expected_name and name != expected_name:
        raise ValueError(f"suite references Dataset/{expected_name}, but {path} contains Dataset/{name}")
    if expected_version and version != expected_version:
        raise ValueError(
            f"suite references Dataset/{expected_name}@{expected_version}, but {path} contains version {version}"
        )
    cases = spec.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset needs at least one case")
    return [_mapping(case) for case in cases if isinstance(case, dict)]


def _suite_settings(path: Path, dataset: Path | None) -> tuple[Path, str, str, float]:
    document = _load_yaml(path)
    if document.get("kind") != "EvalSuite":
        raise ValueError(f"{path} is not an EvalSuite artifact")
    spec = _mapping(document.get("spec"))
    reference = _mapping(spec.get("datasetRef"))
    name = str(reference.get("ref") or "")
    version = str(reference.get("version") or "")
    if not name or not version:
        raise ValueError("eval suite needs a versioned datasetRef")
    threshold = float(spec.get("minimumPassRate", 1.0))
    if not 0 <= threshold <= 1:
        raise ValueError("minimumPassRate must be between 0 and 1")
    if dataset is not None:
        return dataset, name, version, threshold
    if Path(name).name != name:
        raise ValueError(f"invalid dataset reference: {name}")
    catalog_root = path.parent.parent
    candidates = [catalog_root / "datasets" / f"{name}{suffix}" for suffix in (".yaml", ".yml")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate, name, version, threshold
    raise ValueError(f"could not resolve Dataset/{name}@{version} beside {path}")


def _suite_reference(path: Path) -> tuple[str, str]:
    document = _load_yaml(path)
    metadata = _mapping(document.get("metadata"))
    spec = _mapping(document.get("spec"))
    name = str(metadata.get("name") or "")
    version = str(spec.get("version") or metadata.get("tag") or "")
    if not name or not version:
        raise ValueError("eval suite needs a name and immutable version")
    return name, version


def _inline_agent_evals(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if not path.is_file():
        raise ValueError("pass --suite or --dataset when the agent argument is not a YAML file")
    document = _load_yaml(path)
    if document.get("kind") != "Agent":
        raise ValueError(f"{path} is not an Agent artifact")
    spec = _mapping(document.get("spec"))
    metadata = _mapping(document.get("metadata"))
    name = str(metadata.get("name") or spec.get("name") or path.stem)
    cases = spec.get("evals") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError("agent needs inline spec.evals, --suite, or --dataset")
    return name, [_mapping(case) for case in cases if isinstance(case, dict)]


@adk_app.command("test")
def test_agent(
    agent: str = typer.Argument(..., help="Agent name shown in the scorecard."),
    sandbox_id: str = typer.Option("", "--sandbox-id", envvar="DEVAI_SANDBOX_ID"),
    suite: Path | None = typer.Option(None, "--suite"),
    dataset: Path | None = typer.Option(None, "--dataset"),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a saved dataset and return non-zero when any case fails."""
    if not sandbox_id:
        console.print("[red]--sandbox-id is required[/]")
        raise typer.Exit(code=2)
    try:
        minimum_pass_rate = 1.0
        expected_name = ""
        expected_version = ""
        dataset_path = dataset
        scorecard_name = agent
        suite_reference: tuple[str, str] | None = None
        cases: list[dict[str, Any]]
        if suite is not None:
            dataset_path, expected_name, expected_version, minimum_pass_rate = _suite_settings(suite, dataset)
            suite_reference = _suite_reference(suite)
        if suite_reference is not None:
            cases = []
        elif dataset_path is None:
            scorecard_name, cases = _inline_agent_evals(Path(agent))
        else:
            cases = _dataset_cases(
                dataset_path,
                expected_name=expected_name,
                expected_version=expected_version,
            )
        client = _new_sandbox_client(api_url=api_url)
        try:
            if suite_reference is not None:
                run = client.evaluate(sandbox_id, *suite_reference)
            else:
                run = client.test(sandbox_id, cases)
        finally:
            client.close()
    except (AdkError, OSError, ValueError) as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error

    run = scrub_structure(run)
    if json_output or output == "json":
        console.print_json(json.dumps(run))
        summary = run.get("summary") or {}
        if float(summary.get("pass_rate") or 0) < minimum_pass_rate:
            raise typer.Exit(code=2)
        return

    summary = run.get("summary") or {}
    table = Table("case", "result", "failure", title=f"{scorecard_name} evaluation")
    for result in run.get("results") or []:
        failures = "; ".join(str(item) for item in result.get("failures") or [])
        table.add_row(str(result.get("name") or ""), "pass" if result.get("passed") else "fail", failures)
    console.print(table)
    console.print(
        f"pass rate {float(summary.get('pass_rate') or 0) * 100:.1f}% · "
        f"cost ${float(summary.get('cost_usd') or 0):.4f} · p95 {int(summary.get('p95_latency_ms') or 0)} ms"
    )
    if run.get("id"):
        console.print(f"durable evaluation run [bold]{run['id']}[/]")
    if float(summary.get("pass_rate") or 0) < minimum_pass_rate:
        raise typer.Exit(code=2)


@adk_app.command("compare")
def compare_runs(
    baseline_run_id: str = typer.Argument(...),
    candidate_run_id: str = typer.Argument(...),
    api_url: str = typer.Option("", "--api-url"),
    output: str = typer.Option("table", "--output"),
) -> None:
    """Compare two durable evaluation runs and name regressions."""
    try:
        client = _new_sandbox_client(api_url=api_url)
        try:
            comparison = client.compare(baseline_run_id, candidate_run_id)
        finally:
            client.close()
    except AdkError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=2) from error
    if output == "json":
        console.print_json(json.dumps(comparison))
        return
    console.print(str(comparison.get("summary") or comparison.get("id") or "comparison complete"))
    regressions = comparison.get("regressions") or []
    if regressions:
        table = Table("case", "baseline", "candidate")
        for regression in regressions:
            table.add_row(
                str(regression.get("case_id") or ""),
                str(regression.get("baseline_passed") or False),
                str(regression.get("candidate_passed") or False),
            )
        console.print(table)


# ---- helpers ---------------------------------------------------------------


def _walk(target: Path) -> Iterator[Path]:
    """Yield every YAML file under `target` (file or directory)."""
    if target.is_file():
        if target.suffix in (".yaml", ".yml"):
            yield target
        return
    if target.is_dir():
        yield from sorted(target.rglob("*.yaml"))
        yield from sorted(target.rglob("*.yml"))


def _builder_for(doc: dict[str, Any]) -> Skill | Prompt | McpServer | Agent | Dataset | EvalSuite:
    """Convert an on-disk YAML envelope into the matching ADK builder."""
    kind = doc.get("kind", "")
    spec = _mapping(doc.get("spec"))
    metadata = _mapping(doc.get("metadata"))
    name = str(metadata.get("name") or spec.get("name") or "")
    if kind == "Skill":
        skill_builder = Skill(name)
        if "description" in spec:
            skill_builder.description(spec["description"])
        if "category" in spec:
            skill_builder.category(spec["category"])
        if "displayName" in spec:
            skill_builder.title(spec["displayName"])
        if "version" in spec:
            skill_builder.version(spec["version"])
        return skill_builder
    if kind == "Prompt":
        prompt_builder = Prompt(name)
        if "version" in spec:
            prompt_builder.version(spec["version"])
        if "description" in spec:
            prompt_builder.description(spec["description"])
        if "template" in spec or "content" in spec:
            prompt_builder.content(spec.get("template") or spec.get("content", ""))
        return prompt_builder
    if kind == "MCPServer":
        mcp_builder = McpServer(name)
        if "description" in spec:
            mcp_builder.description(spec["description"])
        if "version" in spec:
            mcp_builder.version(spec["version"])
        if "endpoint" in spec:
            mcp_builder.remote(spec["endpoint"])
        return mcp_builder
    if kind == "Agent":
        agent_builder = Agent(name)
        if "description" in spec:
            agent_builder.description(spec["description"])
        if "version" in spec:
            agent_builder.version(spec["version"])
        if spec.get("skill"):
            agent_builder.skill(spec["skill"])
        if spec.get("promptRef"):
            agent_builder.prompt(spec["promptRef"])
        for mcp_server in spec.get("mcpServers") or []:
            agent_builder.mcp_server(str(mcp_server))
        llm = _mapping(spec.get("llm"))
        if llm.get("provider") and llm.get("model"):
            agent_builder.model(llm["provider"], llm["model"])
        sandbox = _mapping(spec.get("sandbox"))
        if sandbox:
            tools = _mapping(sandbox.get("tools"))
            limits = _mapping(sandbox.get("limits"))
            dataset_ref = _mapping(sandbox.get("dataset"))
            agent_builder.sandbox(
                default_mode=str(tools.get("default_mode") or "mock"),
                tool_modes=dict(tools.get("overrides") or {}),
                dataset=(str(dataset_ref["ref"]), str(dataset_ref["version"])) if dataset_ref else None,
                max_tokens=int(limits.get("max_tokens") or 100_000),
                max_cost_usd=float(limits.get("max_cost_usd") or 10.0),
                max_wall_clock_s=int(limits.get("max_wall_clock_s") or 900),
                ttl_seconds=int(sandbox.get("ttl_seconds") or 4 * 60 * 60),
            )
        return agent_builder
    if kind == "Dataset":
        dataset_builder = Dataset(name)
        if "version" in spec:
            dataset_builder.version(spec["version"])
        if "description" in spec:
            dataset_builder.description(spec["description"])
        for case in spec.get("cases") or []:
            case_body = _mapping(case)
            expect = _mapping(case_body.get("expect"))
            dataset_builder.case(str(case_body.get("name") or ""), str(case_body.get("input") or ""), **expect)
        return dataset_builder
    if kind == "EvalSuite":
        suite_builder = EvalSuite(name)
        if "version" in spec:
            suite_builder.version(spec["version"])
        if "description" in spec:
            suite_builder.description(spec["description"])
        dataset_ref = _mapping(spec.get("datasetRef"))
        if dataset_ref.get("ref") and dataset_ref.get("version"):
            suite_builder.dataset(str(dataset_ref["ref"]), str(dataset_ref["version"]))
        if "minimumPassRate" in spec:
            suite_builder.minimum_pass_rate(float(spec["minimumPassRate"]))
        suite_builder.scorers(*(str(scorer) for scorer in spec.get("scorers") or []))
        thresholds = _mapping(spec.get("thresholds"))
        if thresholds:
            suite_builder.thresholds(
                success=float(thresholds["success"]) if thresholds.get("success") is not None else None,
                safety=float(thresholds["safety"]) if thresholds.get("safety") is not None else None,
                hallucination=(
                    float(thresholds["hallucination"]) if thresholds.get("hallucination") is not None else None
                ),
                p95_latency_s=(
                    float(thresholds["p95_latency_s"]) if thresholds.get("p95_latency_s") is not None else None
                ),
                cost_per_run_usd=(
                    float(thresholds["cost_per_run_usd"]) if thresholds.get("cost_per_run_usd") is not None else None
                ),
            )
        return suite_builder
    raise ValueError(f"unknown kind: {kind!r}")
