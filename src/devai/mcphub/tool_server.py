"""Per-domain downstream MCP servers (the real tools the Hub federates).

The Hub (docs/agentic/MCP-HUB.md §6) federates downstream MCP servers. These are
those downstreams: small, domain-scoped MCP servers mounted on devai-api that
expose **genuinely runnable** tools.

Scope decision (Phase 6): only tools with a real standalone handler are exposed.
The bulk of the registry's tool *schemas* (security scans, compile/lint/test) are
**pipeline-stage operations** — they run inside a sandbox git worktree during a
pipeline run and cannot be invoked ad-hoc — so they are deliberately NOT served
here. What ships:

  - ``scm``    — the 13 SCM operations from the tool registry, backed by
                 ``SCMToolExecutor`` + an scm client built from settings. Real:
                 read a repo, open a PR, post a review, etc.
  - ``gitops`` — Argo CD / Kargo / Flux operations (list/status/sync/promote/
                 reconcile/rollback) backed by the ``adapters.gitops`` family.
  - ``sample`` — ``sample_ping`` / ``sample_echo``, a trivial always-works domain
                 to prove end-to-end federation.

Every tool listed here actually runs. The ``mcp`` SDK is imported lazily so this
module (and its pure logic) imports without it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from devai.mcp_stateless import stateless_asgi

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

# An async tool handler: takes the call arguments, returns a string result.
Handler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class DomainTool:
    """One runnable tool a domain server exposes."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


# --------------------------------------------------------------------------- #
# Domains
# --------------------------------------------------------------------------- #


def scm_domain(settings: Settings) -> list[DomainTool]:
    """The SCM domain: every ``scm_*`` tool in the registry, bound to a live
    scm client. Returns [] (degrade, don't crash) if the client can't be built.

    Each tool's handler routes through ``SCMToolExecutor`` (the same path the
    pipeline uses), so behavior is identical to in-run SCM calls. The MCP caller
    supplies ``repo`` (and ``path``/``branch`` etc.) in the call arguments.
    """
    from devai.tools import registry as tool_registry

    try:
        from devai.scm.factory import create_scm_client

        scm = create_scm_client(settings)
    except Exception:  # noqa: BLE001 — no creds / unsupported provider → no SCM domain
        logger.warning("mcphub: SCM domain unavailable (scm client build failed)", exc_info=True)
        return []

    ctx = tool_registry.ToolContext(scm=scm, agent_name="mcp-hub")
    names = sorted(n for n in tool_registry.known() if n.startswith("scm_"))
    out: list[DomainTool] = []
    for bt in tool_registry.bind(names, ctx):
        out.append(
            DomainTool(
                name=bt.spec.name,
                description=bt.spec.description,
                input_schema=bt.spec.parameters or {"type": "object", "properties": {}},
                handler=bt.handler,
            )
        )
    logger.info("mcphub: scm domain exposes %d tools", len(out))
    return out


def gitops_domain(settings: Settings) -> list[DomainTool]:
    """The GitOps domain: Argo CD / Kargo / Flux operations from the tool
    registry, backed by the ``adapters.gitops`` family (kubectl + CRD RBAC).

    Providers come from ``settings.gitops_mcp_providers``; a provider whose
    controller isn't reachable simply contributes no tools (each backend
    degrades independently, and a tool call against a missing controller
    answers with a readable error rather than raising).
    """
    from devai.tools import registry as tool_registry

    raw = str(getattr(settings, "gitops_mcp_providers", "") or "argocd,kargo,flux")
    prefixes = tuple(f"{p.strip().lower()}_" for p in raw.split(",") if p.strip())
    if not prefixes:
        return []

    ctx = tool_registry.ToolContext(agent_name="mcp-hub")
    names = sorted(n for n in tool_registry.known() if n.startswith(prefixes))
    out = [
        DomainTool(
            name=bt.spec.name,
            description=bt.spec.description,
            input_schema=bt.spec.parameters or {"type": "object", "properties": {}},
            handler=bt.handler,
        )
        for bt in tool_registry.bind(names, ctx)
    ]
    logger.info("mcphub: gitops domain exposes %d tools (%s)", len(out), raw)
    return out


def sample_domain() -> list[DomainTool]:
    """A trivial domain that always works — proves end-to-end federation."""

    async def ping(_: dict[str, Any]) -> str:
        return "pong"

    async def echo(args: dict[str, Any]) -> str:
        return str(args.get("text", ""))

    return [
        DomainTool(
            name="sample_ping",
            description="Health check — returns 'pong'.",
            input_schema={"type": "object", "properties": {}},
            handler=ping,
        ),
        DomainTool(
            name="sample_echo",
            description="Echo back the provided text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=echo,
        ),
    ]


# Domain registry: mount path segment → builder. The Hub's MCPServer seeds point
# at ``…/mcp/<segment>`` (scm-mcp → /mcp/scm, sample-mcp → /mcp/sample).
def build_domains(settings: Settings) -> dict[str, list[DomainTool]]:
    """Build all configured domains. Empty domains are dropped."""
    domains = {
        "scm": scm_domain(settings),
        "gitops": gitops_domain(settings),
        "sample": sample_domain(),
    }
    return {seg: tools for seg, tools in domains.items() if tools}


# --------------------------------------------------------------------------- #
# MCP server construction (lazy SDK)
# --------------------------------------------------------------------------- #


def build_domain_server(name: str, tools: list[DomainTool]) -> Any:
    """Wire a low-level MCP ``Server`` exposing ``tools``.

    Mirrors the Hub's own server wiring: dynamic ``tools/list`` + a
    name-dispatched ``tools/call``. Raises ``ImportError`` if the ``mcp`` SDK is
    absent (the caller leaves the mount off).
    """
    import mcp.types as t  # lazy
    from mcp.server.lowlevel import Server  # lazy

    by_name = {tool.name: tool for tool in tools}

    async def _list_tools(_ctx: Any, _params: Any) -> Any:
        return t.ListToolsResult(
            tools=[
                t.Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema) for tool in tools
            ]
        )

    async def _call_tool(_ctx: Any, params: Any) -> Any:
        tool = by_name.get(params.name)
        if tool is None:
            return t.CallToolResult(
                content=[t.TextContent(type="text", text=f"ERROR: unknown tool {params.name!r}")], is_error=True
            )
        result = await tool.handler(params.arguments or {})
        return t.CallToolResult(
            content=[t.TextContent(type="text", text=result if isinstance(result, str) else str(result))]
        )

    return Server(f"devai-{name}-mcp", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def mount_domain_servers(app: Any, settings: Settings) -> list[Any]:
    """Build + mount each domain MCP server at ``/mcp/<segment>`` on ``app``.

    Returns the list of entered session-manager context managers so the caller's
    lifespan can ``__aexit__`` them on shutdown. Best-effort per domain: a domain
    that fails to mount is logged and skipped — never breaks app startup.
    Returns [] if the ``mcp`` SDK is absent.
    """
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError:
        logger.warning("mcphub: mcp SDK absent — downstream domain servers not mounted")
        return []

    entered: list[Any] = []
    for segment, tools in build_domains(settings).items():
        try:
            server = build_domain_server(segment, tools)
            # stateless=True: devai-api runs multiple replicas with no session
            # affinity, so a stateful streamable-HTTP session would break when a
            # follow-up request lands on a different pod ("Session terminated").
            # These domains are pure request/response (no reverse-path), so each
            # call being self-contained is exactly right.
            manager = StreamableHTTPSessionManager(app=server, stateless=True)
            cm = manager.run()
            await cm.__aenter__()
            entered.append(cm)

            app.mount(f"/mcp/{segment}", stateless_asgi(manager.handle_request, normalize_mount=True))
            logger.info("mcphub: mounted downstream domain /mcp/%s (%d tools)", segment, len(tools))
        except Exception:  # noqa: BLE001 — one bad domain must not break startup
            logger.exception("mcphub: failed to mount domain /mcp/%s", segment)
    return entered
