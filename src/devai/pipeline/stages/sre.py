"""SRE agent adapter stages.

The SRE agents have a different shape from ALM (constructed with
`(config, memory)` or `(config, db)`, not the BaseAgent quad), so we
don't reuse AgentAdapter — instead these are direct PipelineStage
implementations.

Outputs land under `sre_<agent>_output` in the handover bag so a
correlator stage downstream can read them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)


class _SREAgentStage(PipelineStage):
    """Common shape: construct an SRE agent, call agent.run(), capture
    the dict result under a handover key."""

    role_key: str = ""  # subclass override
    stage_name: str = ""

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return self.stage_name

    def _make_agent(self) -> Any | None:
        """Subclass returns the constructed agent, or None when deps missing."""
        return None

    def _next_state(self) -> TaskState | None:
        return None

    async def execute(self, task: DevAITask) -> StageResult:
        try:
            agent = self._make_agent()
        except Exception as e:  # noqa: BLE001 — missing optional dep
            logger.warning("SRE stage %s: _make_agent raised %s", self.stage_name, e)
            agent = None
        if agent is None:
            return StageResult(
                next_state=self._next_state(),
                message=f"{self.stage_name} skipped — deps unavailable",
                data={f"{self.role_key}_stub": True},
            )

        # SRE agents take varied args. We call run() with no positional
        # args by default; if the agent signature needs more, the subclass
        # overrides _call().
        try:
            result = await self._call(agent, task)
        except Exception:  # noqa: BLE001
            logger.exception("SRE stage %s failed", self.stage_name)
            raise

        return StageResult(
            next_state=self._next_state(),
            message=str(result.get("summary", "")) if isinstance(result, dict) else "",
            data={f"{self.role_key}_output": result},
        )

    async def _call(self, agent: Any, task: DevAITask) -> Any:
        return await agent.run()


# ──────────────────────────────────────────────────────────────────────


class _DiscoverStage(_SREAgentStage):
    stage_name = "sre_discover"
    role_key = "discovery"

    def _next_state(self) -> TaskState:
        return TaskState.DISCOVERING

    def _make_agent(self):
        if self.deps.config is None:
            return None
        try:
            from devai.sre.agents.discovery_agent import DiscoveryAgent
            return DiscoveryAgent(self.deps.config, memory=getattr(self.deps.state_manager, "memory", None))
        except Exception:  # noqa: BLE001
            logger.exception("DiscoveryAgent construction failed")
            return None


def discover_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DiscoverStage(deps, config)


def _build_monitor_stage(stage_name: str, role_key: str, klass_path: str):
    class _MonitorStage(_SREAgentStage):
        pass

    _MonitorStage.stage_name = stage_name
    _MonitorStage.role_key = role_key

    def _next_state(self):
        return TaskState.MONITORING

    def _make_agent(self):
        if self.deps.config is None:
            return None
        try:
            module_path, class_name = klass_path.rsplit(".", 1)
            import importlib
            mod = importlib.import_module(module_path)
            klass = getattr(mod, class_name)
            return klass(self.deps.config, memory=getattr(self.deps.state_manager, "memory", None))
        except TypeError:
            # Fallback for agents whose ctor doesn't accept memory=
            try:
                return klass(self.deps.config)  # type: ignore[name-defined]
            except Exception:  # noqa: BLE001
                logger.exception("monitor agent construction failed")
                return None
        except Exception:  # noqa: BLE001
            logger.exception("monitor agent construction failed")
            return None

    _MonitorStage._next_state = _next_state  # type: ignore[method-assign]
    _MonitorStage._make_agent = _make_agent  # type: ignore[method-assign]
    return _MonitorStage


_InfraMonitor = _build_monitor_stage("sre_monitor_infra", "infra_monitor", "devai.sre.agents.infra_monitor.InfraMonitorAgent")
_PerfMonitor = _build_monitor_stage("sre_monitor_perf", "perf_monitor", "devai.sre.agents.perf_monitor.PerfMonitorAgent")
_LogMonitor = _build_monitor_stage("sre_monitor_logs", "log_analyzer", "devai.sre.agents.log_analyzer.LogAnalyzerAgent")
_CostMonitor = _build_monitor_stage("sre_monitor_cost", "cost_analyzer", "devai.sre.agents.cost_analyzer.CostAnalyzerAgent")
_CapacityMonitor = _build_monitor_stage("sre_monitor_capacity", "capacity_planner", "devai.sre.agents.capacity_planner.CapacityPlannerAgent")


def monitor_infra_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _InfraMonitor(deps, config)


def monitor_perf_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _PerfMonitor(deps, config)


def monitor_logs_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _LogMonitor(deps, config)


def monitor_cost_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CostMonitor(deps, config)


def monitor_capacity_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CapacityMonitor(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Correlate / respond / learn — synthesize outputs from the parallel monitors.
# ──────────────────────────────────────────────────────────────────────


class _CorrelateStage(PipelineStage):
    """Fan-in for the parallel monitor stages.

    Reads `*_output` keys produced by the monitors and produces a single
    `correlated_findings` blob. The actual cross-signal logic lives in
    `devai.sre.graph` — we lazy-import to keep the SRE optional.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps

    def name(self) -> str:
        return "sre_correlate"

    async def execute(self, task: DevAITask) -> StageResult:
        findings: list[dict[str, Any]] = []
        for key in ("infra_monitor_output", "perf_monitor_output", "log_analyzer_output", "cost_analyzer_output", "capacity_planner_output"):
            entry = task.agent_context.get(key)
            if isinstance(entry, dict) and entry.get("findings"):
                findings.extend(entry["findings"])

        return StageResult(
            next_state=TaskState.CORRELATING,
            message=f"correlated {len(findings)} findings",
            data={"correlated_findings": findings},
        )


def correlate_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CorrelateStage(deps, config)


class _RespondStage(_SREAgentStage):
    stage_name = "sre_respond"
    role_key = "incident_responder"

    def _next_state(self) -> TaskState:
        return TaskState.RESPONDING

    def _make_agent(self):
        if self.deps.config is None:
            return None
        try:
            from devai.sre.agents.incident_responder import IncidentResponderAgent
            db = getattr(self.deps.state_manager, "db", None)
            return IncidentResponderAgent(self.deps.config, db)
        except Exception:  # noqa: BLE001
            logger.exception("IncidentResponderAgent construction failed")
            return None

    async def _call(self, agent: Any, task: DevAITask) -> Any:
        findings = task.agent_context.get("correlated_findings", [])
        # IncidentResponderAgent.run takes findings or similar — try a few shapes.
        try:
            return await agent.run(findings)
        except TypeError:
            return await agent.run()


def respond_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _RespondStage(deps, config)


class _LearnStage(PipelineStage):
    """Capture findings + remediation back into agent memory so future runs
    benefit.

    Writes one episodic memory record summarizing this SRE sweep. Source
    of the memory backend, in priority order:
      1. `deps.memory` — MemoryAdapter chosen by DEVAI_MEMORY_PROVIDER.
      2. `deps.state_manager.memory` — legacy SREOrchestrator path.
      3. Nothing — stage is still a success, just with no learning written.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps

    def name(self) -> str:
        return "sre_learn"

    async def execute(self, task: DevAITask) -> StageResult:
        findings = task.agent_context.get("correlated_findings", []) or []
        response = task.agent_context.get("incident_responder_output")

        summary = (
            f"SRE sweep task={task.id} repo={task.repo or 'n/a'} "
            f"findings={len(findings)} responded={'yes' if response else 'no'}"
        )
        meta = {"findings": findings, "response": response, "task_id": task.id}

        # ── Preferred: MemoryAdapter ───────────────────────────────
        if self.deps.memory is not None:
            try:
                await self.deps.memory.remember(
                    content=summary,
                    agent="sre",
                    repo=task.repo or "global",
                    memory_type="episodic",
                    tags=["sre", "scan", task.blueprint],
                    metadata=meta,
                )
                return StageResult(
                    next_state=TaskState.LEARNING,
                    message=f"learned via {self.deps.memory.provider_name}: {summary}",
                    data={"learn_done": True, "memory_provider": self.deps.memory.provider_name},
                )
            except Exception:  # noqa: BLE001
                logger.exception("memory adapter remember failed — falling back")

        # ── Legacy: state_manager.memory ───────────────────────────
        memory_obj = getattr(self.deps.state_manager, "memory", None) if self.deps.state_manager else None
        if memory_obj is not None and hasattr(memory_obj, "remember"):
            try:
                await memory_obj.remember(
                    agent="sre",
                    content=summary,
                    memory_type="episodic",
                    metadata=meta,
                )
            except Exception:  # noqa: BLE001
                logger.exception("legacy memory.remember failed")

        return StageResult(
            next_state=TaskState.LEARNING,
            message=f"recorded {len(findings)} findings",
            data={"learn_done": True},
        )


def learn_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _LearnStage(deps, config)


__all__ = [
    "correlate_stage",
    "discover_stage",
    "learn_stage",
    "monitor_capacity_stage",
    "monitor_cost_stage",
    "monitor_infra_stage",
    "monitor_logs_stage",
    "monitor_perf_stage",
    "respond_stage",
]
