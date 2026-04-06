"""DevAI ALM Agents — full lifecycle management."""

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

ALL_AGENTS = [
    DocumentAnalyzerAgent,
    TechDetectorAgent,
    RequirementsAnalystAgent,
    ProductDirectorAgent,
    EngineeringManagerAgent,
    SeniorDeveloperAgent,
    DBEngineerAgent,
    StaffReviewerAgent,
    SecurityExpertAgent,
    CIMonitorAgent,
    QATesterAgent,
    InfraProvisionerAgent,
    ReleaseManagerAgent,
]

__all__ = [
    "DocumentAnalyzerAgent",
    "TechDetectorAgent",
    "RequirementsAnalystAgent",
    "ProductDirectorAgent",
    "EngineeringManagerAgent",
    "SeniorDeveloperAgent",
    "DBEngineerAgent",
    "StaffReviewerAgent",
    "SecurityExpertAgent",
    "CIMonitorAgent",
    "QATesterAgent",
    "InfraProvisionerAgent",
    "ReleaseManagerAgent",
    "ALL_AGENTS",
]
