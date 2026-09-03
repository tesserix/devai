"""Per-caller tool-surface budgeting (the ~40-tool ceiling).

An agent practically tops out around ~40 active tools before tool definitions
blow the context budget (docs/agentic/MCP-HUB.md §1, §6.5). So the Hub never
exposes the raw union of every downstream's tools — it resolves a ``ToolProfile``
for the caller (from JWT scope / ``?profile=`` / an Agent's ``allowed_tools``)
and serves only that scoped subset.

The standard this sets: **degrade, never silently drop** (docs §9). When the
selection has to truncate to fit the budget, the dropped tools are reported in
the result and logged — a caller (or the dashboard) can always see what was cut
and raise the budget or narrow the profile.

Pure module: takes a tool list + a profile, returns a deterministic selection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from devai.mcphub.model import FederatedTool

logger = logging.getLogger(__name__)

# The default surface budget. Chosen at the documented practical ceiling; a
# profile may raise it deliberately (auditable, since profiles are artifacts).
DEFAULT_MAX_TOOLS = 40

# Tiers included by default — only the curated `core` set, so a fresh caller
# gets a usable, in-budget surface without opting into everything.
DEFAULT_TIERS = ("core",)


@dataclass(slots=True)
class ToolProfile:
    """A caller's tool-surface scope.

    Precedence when selecting (most specific first):
      1. ``allow`` — explicit namespaced names. If set, ONLY these are eligible
         and they bypass the tier filter (an explicit pin always wins).
      2. ``servers`` — restrict eligibility to these downstream servers.
      3. ``tiers`` — keep tools whose ``devai.io/tier`` is in this set.
    The eligible set is then capped at ``max_tools`` (deterministically).
    """

    name: str = "default"
    tiers: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_TIERS))
    allow: frozenset[str] = field(default_factory=frozenset)  # explicit namespaced names
    servers: frozenset[str] = field(default_factory=frozenset)  # restrict to these servers
    max_tools: int = DEFAULT_MAX_TOOLS

    @classmethod
    def default(cls) -> ToolProfile:
        return cls()

    @classmethod
    def unrestricted(cls, *, max_tools: int = DEFAULT_MAX_TOOLS) -> ToolProfile:
        """All tiers, all servers — still budget-capped. For admin/debug callers."""
        return cls(
            name="unrestricted",
            tiers=frozenset({"core", "extended", "experimental"}),
            max_tools=max_tools,
        )


@dataclass(slots=True)
class BudgetResult:
    """Outcome of applying a profile to a tool catalog."""

    selected: list[FederatedTool]
    dropped_by_filter: list[str]  # names excluded by tier/server/allow
    dropped_by_budget: list[str]  # names cut to fit max_tools (in-budget overflow)

    @property
    def truncated(self) -> bool:
        return bool(self.dropped_by_budget)


def select(tools: list[FederatedTool], profile: ToolProfile) -> BudgetResult:
    """Apply ``profile`` to ``tools`` → a budgeted, deterministic selection.

    Deterministic by sorting on the namespaced name before the budget cut, so a
    given (catalog, profile) always yields the same surface — important for
    cacheability and for repeated stateless discovery to remain stable.
    """
    eligible: list[FederatedTool] = []
    dropped_filter: list[str] = []

    for t in tools:
        if profile.allow:
            keep = t.name in profile.allow  # explicit pin wins, bypasses tiers
        elif (profile.servers and t.server not in profile.servers) or (profile.tiers and t.tier not in profile.tiers):
            keep = False  # excluded by server restriction or tier filter
        else:
            keep = True
        if keep:
            eligible.append(t)
        else:
            dropped_filter.append(t.name)

    eligible.sort(key=lambda t: t.name)

    dropped_budget: list[str] = []
    cap = max(0, profile.max_tools)
    if len(eligible) > cap:
        dropped_budget = [t.name for t in eligible[cap:]]
        eligible = eligible[:cap]
        logger.warning(
            "mcphub: profile %r truncated tool surface to budget: kept %d, dropped %d over the "
            "%d-tool ceiling (%s). Narrow the profile or raise max_tools.",
            profile.name,
            len(eligible),
            len(dropped_budget),
            cap,
            ", ".join(dropped_budget[:8]) + ("…" if len(dropped_budget) > 8 else ""),
        )

    return BudgetResult(
        selected=eligible,
        dropped_by_filter=dropped_filter,
        dropped_by_budget=dropped_budget,
    )
