"""Central tool registry — turns a spec's `allowed_tools` into a real gate.

Today a specialization YAML declares `allowed_tools: [...]` but nothing
enforces it: the legacy agents hand a fixed tool list to Claude. The
Agent Runner (`devai.agentruntime.runner`) changes that — it asks this
registry to `bind(spec.allowed_tools, ctx)` and gets back ONLY the tools
the spec is allowed to use, each as a `(ToolSpec, async handler)` pair the
runner can advertise to the LLM and dispatch.

Shape (mirrors the adapter pattern):

    _REGISTRY: dict[name -> RegisteredTool]
    RegisteredTool = { spec: ToolSpec, factory: (ToolContext) -> Handler }
    Handler        = async (args: dict) -> str

A tool's `factory` is called at bind time with the per-run `ToolContext`,
so handlers close over the live SCM client / workdir / adapters instead of
reaching into globals. New tool families (shell, web, checkpoint) call
`register()` at import time and immediately become bind-able — the spec's
`allowed_tools` list is the only thing that decides whether an agent gets
them.

The existing dict-shaped tool schemas (`SCM_TOOLS`, `FILE_TOOLS`) are
adopted as-is: their `input_schema` becomes the `ToolSpec.parameters`, and
execution routes through the existing `SCMToolExecutor` (guardrails +
secret masking + dashboard repo-event fan-out preserved).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devai.adapters.llm.base import ToolSpec

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)

# A bound tool handler: takes the model-supplied args, returns a string the
# runner feeds back to the LLM as the tool result.
Handler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class ToolContext:
    """Per-run wiring handed to a tool factory at bind time.

    Every field is optional so a tool that doesn't need (say) the object
    store still binds. Handlers MUST tolerate the bits they use being None
    and return a clear error string rather than raising — a tool failure
    should not crash the agent loop.
    """

    # Identity / attribution (flows into guardrails + audit + repo events)
    repo: str = ""
    branch: str = ""
    run_id: str = ""
    agent_name: str = ""
    triggered_by: str = ""
    trace_id: str = ""

    # Live integrations
    scm: SCMClient | None = None
    redis: Any = None
    llm: LLMAdapter | None = None
    web_search: Any = None  # WebSearchAdapter (Phase 1)
    object_store: Any = None  # ObjectStoreAdapter (Phase 1)

    # Sandbox working tree (Phase 1 — shell/checkpoint tools run here).
    # In-pod the runner sets this to the git worktree path.
    workdir: str = ""

    # Free-form extras for niche tools.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RegisteredTool:
    spec: ToolSpec
    factory: Callable[[ToolContext], Handler]


@dataclass(slots=True, frozen=True)
class BoundTool:
    """A tool resolved against a ToolContext, ready for the runner."""

    spec: ToolSpec
    handler: Handler


_REGISTRY: dict[str, RegisteredTool] = {}


def register(
    name: str,
    description: str,
    parameters: dict[str, Any],
    factory: Callable[[ToolContext], Handler],
    *,
    overwrite: bool = False,
) -> None:
    """Register a single tool. Idempotent unless `overwrite` is set."""
    if name in _REGISTRY and not overwrite:
        return
    _REGISTRY[name] = RegisteredTool(
        spec=ToolSpec(
            name=name, description=description, parameters=parameters or {"type": "object", "properties": {}}
        ),
        factory=factory,
    )


def register_schema_dicts(
    schemas: list[dict[str, Any]],
    factory: Callable[[ToolContext], Handler],
    *,
    overwrite: bool = False,
) -> None:
    """Adopt a list of legacy `{name, description, input_schema}` dicts.

    Every tool shares one `factory`; the factory's handler dispatches by
    name internally (that's how `SCMToolExecutor.execute` already works).
    """
    for schema in schemas:
        register(
            schema["name"],
            schema.get("description", ""),
            schema.get("input_schema", {}),
            factory,
            overwrite=overwrite,
        )


def known() -> list[str]:
    return sorted(_REGISTRY)


def has(name: str) -> bool:
    return name in _REGISTRY


def get(name: str) -> RegisteredTool | None:
    """Look up one registered tool (spec + factory) by name."""
    return _REGISTRY.get(name)


def bind(allowed_tools: list[str], ctx: ToolContext) -> list[BoundTool]:
    """Resolve a spec's allowed_tools against the registry + a context.

    Unknown names are skipped with a warning (a spec referencing a tool we
    don't ship should degrade, not crash). The returned order matches
    `allowed_tools`.
    """
    bound: list[BoundTool] = []
    seen: set[str] = set()
    for name in allowed_tools:
        if name in seen:
            continue
        seen.add(name)
        entry = _REGISTRY.get(name)
        if entry is None:
            logger.warning("tool %r requested by spec but not registered — skipping", name)
            continue
        try:
            handler = entry.factory(ctx)
        except Exception:  # noqa: BLE001 — a broken factory shouldn't kill the bind
            logger.exception("tool %r factory raised at bind time — skipping", name)
            continue
        bound.append(BoundTool(spec=entry.spec, handler=handler))
    return bound


# ─────────────────────────────────────────────────────────────────────────
# Built-in tool families
# ─────────────────────────────────────────────────────────────────────────

# Map the FILE_TOOLS names onto the SCM executor's handlers so an agent can
# use either vocabulary. The executor has no _handle_read_file etc., so we
# translate the call into the equivalent scm_* operation.
_FILE_TO_SCM = {
    "read_file": "scm_get_file_content",
    "write_file": "scm_commit_file",
    "list_directory": "scm_list_files",
}


def _scm_factory(ctx: ToolContext) -> Handler:
    """Build one SCMToolExecutor for this context and route scm_*/file tools."""

    async def handler(args: dict[str, Any]) -> str:
        if ctx.scm is None:
            return "ERROR: no SCM client available in this run"
        from devai.tools.scm_tools import SCMToolExecutor

        executor = SCMToolExecutor(
            ctx.scm,
            ctx.agent_name,
            run_id=ctx.run_id,
            redis=ctx.redis,
            triggered_by=ctx.triggered_by,
            trace_id=ctx.trace_id,
        )
        # `args` carries a "_tool" injected by the dispatcher (the real
        # scm_* name to run).
        tool_name = args.pop("_tool", None) or args.pop("tool", None) or ""
        # Fill repo/branch from context when the model omitted them — harmless
        # extra keys are ignored by handlers that don't read them.
        if ctx.repo:
            args.setdefault("repo", ctx.repo)
        if ctx.branch:
            args.setdefault("branch", ctx.branch)
        try:
            return await executor.execute(tool_name, args)
        except Exception as e:  # noqa: BLE001
            return f"ERROR executing {tool_name}: {e}"

    return handler


def _bind_named(real_name: str, factory: Callable[[ToolContext], Handler]) -> Callable[[ToolContext], Handler]:
    """Wrap a shared factory so the produced handler knows its own tool name.

    Each registered tool needs a handler that calls the executor with the
    RIGHT tool name. We curry the name in by injecting it into args.
    """

    def make(ctx: ToolContext) -> Handler:
        inner = factory(ctx)

        async def handler(args: dict[str, Any]) -> str:
            enriched = dict(args)
            enriched["_tool"] = real_name
            return await inner(enriched)

        return handler

    return make


def register_builtin_tools(*, overwrite: bool = False) -> None:
    """Register the SCM + file tool families. Safe to call repeatedly."""
    from devai.tools.file_tools import FILE_TOOLS
    from devai.tools.scm_tools import SCM_TOOLS

    for schema in SCM_TOOLS:
        register(
            schema["name"],
            schema.get("description", ""),
            schema.get("input_schema", {}),
            _bind_named(schema["name"], _scm_factory),
            overwrite=overwrite,
        )

    for schema in FILE_TOOLS:
        scm_equivalent = _FILE_TO_SCM.get(schema["name"], schema["name"])
        register(
            schema["name"],
            schema.get("description", ""),
            schema.get("input_schema", {}),
            _bind_named(scm_equivalent, _scm_factory),
            overwrite=overwrite,
        )


def register_capability_tools(*, overwrite: bool = False) -> None:
    """Import the capability tool families so they self-register.

    web/shell/checkpoint tools register on import; pulling them in here
    means a `bind()` right after importing the registry sees the full
    Cursor-grade tool set (terminal, web, git checkpoints).
    """
    import devai.tools.checkpoint_tools  # noqa: F401
    import devai.tools.gitops_tools  # noqa: F401
    import devai.tools.shell_tools  # noqa: F401
    import devai.tools.web_tools  # noqa: F401


# Register the built-ins at import time so any `bind()` after import works.
register_builtin_tools()
register_capability_tools()


__all__ = [
    "BoundTool",
    "Handler",
    "RegisteredTool",
    "ToolContext",
    "bind",
    "get",
    "has",
    "known",
    "register",
    "register_builtin_tools",
    "register_schema_dicts",
]
