"""ALM agent adapter stages.

Each factory in this module wraps one existing class from `devai.agents/*`
behind the PipelineStage interface. The agents themselves are unchanged —
this layer just translates DevAITask ↔ ALMState, runs `agent.run()`, and
stuffs the patch back into the handover bag.

Long-term these go away in favor of YAML specializations + LLM provider
dispatch, but for now this is how the blueprint runtime drives the 14
existing ALM agents.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.stages._base import AgentAdapter, _safe_agent
from devai.pipeline.types import TaskState

logger = logging.getLogger(__name__)


def _make(klass: Any, deps: StageDeps) -> Any | None:
    return _safe_agent(klass, deps)


# ──────────────────────────────────────────────────────────────────────
# Planning chain — ingest → tech → analyze → epic → stories → plan
# ──────────────────────────────────────────────────────────────────────


class _IngestDocumentsStage(AgentAdapter):
    def name(self) -> str:
        return "ingest_documents"

    def role_key(self) -> str:
        return "document_analyzer"

    def _next_state(self) -> TaskState:
        return TaskState.INGESTING

    def _make_agent(self):
        from devai.agents.document_analyzer import DocumentAnalyzerAgent
        return _make(DocumentAnalyzerAgent, self.deps)


def ingest_documents_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _IngestDocumentsStage(deps, config)


class _DetectTechStackStage(AgentAdapter):
    def name(self) -> str:
        return "detect_tech_stack"

    def role_key(self) -> str:
        return "tech_detector"

    def _next_state(self) -> TaskState:
        return TaskState.ANALYZING

    def _make_agent(self):
        from devai.agents.tech_detector import TechDetectorAgent
        return _make(TechDetectorAgent, self.deps)


def detect_tech_stack_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DetectTechStackStage(deps, config)


class _AnalyzeRequirementsStage(AgentAdapter):
    def name(self) -> str:
        return "analyze_requirements"

    def role_key(self) -> str:
        return "requirements_analyst"

    def _next_state(self) -> TaskState:
        return TaskState.ANALYZING

    def _make_agent(self):
        from devai.agents.requirements_analyst import RequirementsAnalystAgent
        return _make(RequirementsAnalystAgent, self.deps)


def analyze_requirements_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _AnalyzeRequirementsStage(deps, config)


class _CreateEpicStage(AgentAdapter):
    def name(self) -> str:
        return "create_epic"

    def role_key(self) -> str:
        return "product_director"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        from devai.agents.product_director import ProductDirectorAgent
        return _make(ProductDirectorAgent, self.deps)


def create_epic_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreateEpicStage(deps, config)


class _CreateStoriesStage(AgentAdapter):
    """The current ProductDirector creates both epic and stories in a single
    run; this stage exists so a blueprint can split them logically. By
    default we reuse the product_director_output written by create_epic.
    """

    def name(self) -> str:
        return "create_stories"

    def role_key(self) -> str:
        return "story_creator"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        # No standalone agent — this is a sub-stage of product_director.
        # We return None and rely on the handover from create_epic.
        return None


def create_stories_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreateStoriesStage(deps, config)


class _CreatePlanStage(AgentAdapter):
    def name(self) -> str:
        return "create_plan"

    def role_key(self) -> str:
        return "engineering_manager"

    def _next_state(self) -> TaskState:
        return TaskState.PLANNING

    def _make_agent(self):
        from devai.agents.engineering_manager import EngineeringManagerAgent
        return _make(EngineeringManagerAgent, self.deps)


def create_plan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _CreatePlanStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Implementation chain
# ──────────────────────────────────────────────────────────────────────


class _ImplementCodeStage(AgentAdapter):
    def name(self) -> str:
        return "implement_code"

    def role_key(self) -> str:
        return "senior_developer"

    def _next_state(self) -> TaskState:
        return TaskState.IMPLEMENTING

    def _make_agent(self):
        from devai.agents.senior_developer import SeniorDeveloperAgent
        return _make(SeniorDeveloperAgent, self.deps)


def implement_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ImplementCodeStage(deps, config)


class _DBEngineeringStage(AgentAdapter):
    def name(self) -> str:
        return "db_engineering"

    def role_key(self) -> str:
        return "db_engineer"

    def _next_state(self) -> TaskState:
        return TaskState.IMPLEMENTING

    def _make_agent(self):
        from devai.agents.db_engineer import DBEngineerAgent
        return _make(DBEngineerAgent, self.deps)


def db_engineering_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DBEngineeringStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Quality gates
# ──────────────────────────────────────────────────────────────────────


class _ReviewCodeStage(AgentAdapter):
    def name(self) -> str:
        return "review_code"

    def role_key(self) -> str:
        return "staff_reviewer"

    def _next_state(self) -> TaskState:
        return TaskState.REVIEWING

    def _make_agent(self):
        from devai.agents.staff_reviewer import StaffReviewerAgent
        return _make(StaffReviewerAgent, self.deps)


def review_code_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ReviewCodeStage(deps, config)


class _StaffReviewStage(AgentAdapter):
    """Final review gate — same agent as review_code but distinct stage so
    a blueprint can run a smoke-review early and a staff review late."""

    def name(self) -> str:
        return "staff_review"

    def role_key(self) -> str:
        return "staff_reviewer_final"

    def _next_state(self) -> TaskState:
        return TaskState.REVIEWING

    def _make_agent(self):
        from devai.agents.staff_reviewer import StaffReviewerAgent
        return _make(StaffReviewerAgent, self.deps)


def staff_review_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _StaffReviewStage(deps, config)


class _SecurityScanStage(AgentAdapter):
    def name(self) -> str:
        return "security_scan"

    def role_key(self) -> str:
        return "security_expert"

    def _next_state(self) -> TaskState:
        return TaskState.SECURITY_SCANNING

    def _make_agent(self):
        from devai.agents.security_expert import SecurityExpertAgent
        return _make(SecurityExpertAgent, self.deps)


def security_scan_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _SecurityScanStage(deps, config)


class _MonitorBuildStage(AgentAdapter):
    def name(self) -> str:
        return "monitor_build"

    def role_key(self) -> str:
        return "ci_monitor"

    def _next_state(self) -> TaskState:
        return TaskState.BUILDING

    def _make_agent(self):
        from devai.agents.ci_monitor import CIMonitorAgent
        return _make(CIMonitorAgent, self.deps)


def monitor_build_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _MonitorBuildStage(deps, config)


class _RunTestsStage(AgentAdapter):
    def name(self) -> str:
        return "run_tests"

    def role_key(self) -> str:
        return "qa_tester"

    def _next_state(self) -> TaskState:
        return TaskState.TESTING

    def _make_agent(self):
        from devai.agents.qa_tester import QATesterAgent
        return _make(QATesterAgent, self.deps)


def run_tests_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _RunTestsStage(deps, config)


# ──────────────────────────────────────────────────────────────────────
# Deployment chain
# ──────────────────────────────────────────────────────────────────────


class _ProvisionInfraStage(AgentAdapter):
    def name(self) -> str:
        return "provision_infra"

    def role_key(self) -> str:
        return "infra_provisioner"

    def _next_state(self) -> TaskState:
        return TaskState.PROVISIONING

    def _make_agent(self):
        from devai.agents.infra_provisioner import InfraProvisionerAgent
        return _make(InfraProvisionerAgent, self.deps)


def provision_infra_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _ProvisionInfraStage(deps, config)


class _DeployReleaseStage(AgentAdapter):
    def name(self) -> str:
        return "deploy_release"

    def role_key(self) -> str:
        return "release_manager"

    def _next_state(self) -> TaskState:
        return TaskState.DEPLOYING

    def _make_agent(self):
        from devai.agents.release_manager import ReleaseManagerAgent
        return _make(ReleaseManagerAgent, self.deps)


def deploy_release_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _DeployReleaseStage(deps, config)


__all__ = [
    "analyze_requirements_stage",
    "create_epic_stage",
    "create_plan_stage",
    "create_stories_stage",
    "db_engineering_stage",
    "deploy_release_stage",
    "detect_tech_stack_stage",
    "implement_code_stage",
    "ingest_documents_stage",
    "monitor_build_stage",
    "provision_infra_stage",
    "review_code_stage",
    "run_tests_stage",
    "security_scan_stage",
    "staff_review_stage",
]
