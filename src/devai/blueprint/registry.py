"""Stage registry — the lookup table from `stage:` YAML key → factory.

The registry decouples blueprint authoring from stage implementation: a
YAML file references stages by string, and the runtime resolves those at
load time. New stages can be registered by external code (plugins, tests)
without touching this file.

Mirrors `internal/blueprint/registry.go::RegisterDefaults` in Fiber.
"""

from __future__ import annotations

import logging

from devai.pipeline.interfaces import PipelineStage, StageDeps, StageFactory

logger = logging.getLogger(__name__)


class StageRegistryError(Exception):
    """Raised when a stage key collides or fails to resolve."""


class StageRegistry:
    """Map of stage key → factory.

    Multiple StageRegistry instances can coexist (e.g. one for the prod
    runtime, one with mocks for tests). The Pipeline owns its registry
    and hands it to the BlueprintExecutor.
    """

    def __init__(self) -> None:
        self._factories: dict[str, StageFactory] = {}

    def register(self, key: str, factory: StageFactory) -> None:
        if key in self._factories:
            raise StageRegistryError(f"stage {key!r} already registered")
        self._factories[key] = factory

    def register_or_replace(self, key: str, factory: StageFactory) -> None:
        """For tests / overrides."""
        self._factories[key] = factory

    def resolve(self, key: str, deps: StageDeps, config: dict[str, str]) -> PipelineStage:
        try:
            factory = self._factories[key]
        except KeyError as e:
            available = ", ".join(sorted(self._factories.keys()))
            raise StageRegistryError(f"unknown stage {key!r}. Known stages: {available}") from e
        return factory(deps, config)

    def has(self, key: str) -> bool:
        return key in self._factories

    def known_stages(self) -> list[str]:
        return sorted(self._factories.keys())


def register_defaults(registry: StageRegistry) -> None:
    """Wire every built-in DevAI stage into the registry.

    Imports happen inside the function so a missing optional dep (e.g.
    the SRE subsystem) never breaks the ALM-only setup.
    """
    # Lazy imports so this file stays cheap and circular-import-free.
    from devai.pipeline.stages import alm as _alm
    from devai.pipeline.stages import crew_runner as _crew_runner
    from devai.pipeline.stages import job_runner as _job_runner
    from devai.pipeline.stages import lifecycle as _lifecycle
    from devai.pipeline.stages import preview as _preview
    from devai.pipeline.stages import specialization as _specialization
    from devai.pipeline.stages import sre as _sre

    # ─── Lifecycle ────────────────────────────────────────────────────
    registry.register("create_issue", _lifecycle.create_issue_stage)
    registry.register("detect_pr", _lifecycle.detect_pr_stage)
    registry.register("await_merge", _lifecycle.await_merge_stage)
    registry.register("cleanup", _lifecycle.cleanup_stage)
    registry.register("context_hydration", _lifecycle.context_hydration_stage)
    registry.register("memory_injection", _lifecycle.memory_injection_stage)
    registry.register("post_report", _lifecycle.post_report_stage)
    registry.register("noop", _lifecycle.noop_stage)

    # ─── ALM agent adapters ──────────────────────────────────────────
    registry.register("ingest_documents", _alm.ingest_documents_stage)
    registry.register("detect_tech_stack", _alm.detect_tech_stack_stage)
    registry.register("analyze_requirements", _alm.analyze_requirements_stage)
    registry.register("create_epic", _alm.create_epic_stage)
    registry.register("create_stories", _alm.create_stories_stage)
    registry.register("create_plan", _alm.create_plan_stage)
    registry.register("implement_code", _alm.implement_code_stage)
    registry.register("db_engineering", _alm.db_engineering_stage)
    registry.register("review_code", _alm.review_code_stage)
    registry.register("security_scan", _alm.security_scan_stage)
    registry.register("monitor_build", _alm.monitor_build_stage)
    registry.register("run_tests", _alm.run_tests_stage)
    registry.register("provision_infra", _alm.provision_infra_stage)
    registry.register("deploy_release", _alm.deploy_release_stage)
    registry.register("staff_review", _alm.staff_review_stage)

    # ─── Specialization runner (YAML role catalog) ───────────────────
    registry.register("run_specialization", _specialization.run_specialization_stage)

    # ─── Crew runner (AI agent teams) ────────────────────────────────
    # Blueprints declare `stage: run_crew` (+ optional `config.crew: <name>`).
    # The lead plans, members (each an AgentRunner) execute, via crews/*.yaml
    # seeds or a DB-backed crew assigned to the task (task.crew_id).
    registry.register("run_crew", _crew_runner.crew_runner_stage)

    # ─── K8s Job runner (default once k8s_runtime_enabled=true) ──────
    # Blueprints declare `stage: run_as_job` + `config.agent: <name>` to
    # execute an agent in a dedicated Pod. The Pod resolves its skills
    # and prompts from aregistry at boot. See pipeline/stages/job_runner.py.
    registry.register("run_as_job", _job_runner.job_runner_stage)

    # ─── Preview pod (Deployment + Service + Istio VirtualService) ───
    # The ``spin_preview_pod`` stage applies the manifests built by
    # ``build_preview_manifests`` directly from the orchestrator pod.
    # Runs inline, not as a Job — see preview.py for the rationale.
    registry.register("spin_preview_pod", _preview.spin_preview_pod_stage)

    # ─── SRE agent adapters ──────────────────────────────────────────
    registry.register("sre_discover", _sre.discover_stage)
    registry.register("sre_monitor_infra", _sre.monitor_infra_stage)
    registry.register("sre_monitor_perf", _sre.monitor_perf_stage)
    registry.register("sre_monitor_logs", _sre.monitor_logs_stage)
    registry.register("sre_monitor_cost", _sre.monitor_cost_stage)
    registry.register("sre_monitor_capacity", _sre.monitor_capacity_stage)
    registry.register("sre_correlate", _sre.correlate_stage)
    registry.register("sre_respond", _sre.respond_stage)
    registry.register("sre_learn", _sre.learn_stage)

    logger.info("registered %d default stages", len(registry.known_stages()))


__all__ = ["StageRegistry", "StageRegistryError", "register_defaults"]
