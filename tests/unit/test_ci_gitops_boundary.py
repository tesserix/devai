from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
BUILD_WORKFLOWS = (
    ".github/workflows/agent-evals.yaml",
    ".github/workflows/auth-bff-build.yaml",
    ".github/workflows/ci.yaml",
    ".github/workflows/dashboard-build.yaml",
    ".github/workflows/sre-build.yaml",
    ".github/workflows/sre-dashboard-build.yaml",
)
FORBIDDEN_DEPLOYMENT_CAPABILITIES = (
    "GCP_PROJECT_ID:",
    "GKE_CLUSTER:",
    "GKE_REGION:",
    "google-github-actions/auth@",
    "google-github-actions/get-gke-credentials@",
    "id-token: write",
    "kubectl rollout restart",
)


@pytest.mark.parametrize("workflow", BUILD_WORKFLOWS)
def test_image_builds_cannot_mutate_production(workflow: str) -> None:
    content = (REPOSITORY_ROOT / workflow).read_text()
    forbidden = [capability for capability in FORBIDDEN_DEPLOYMENT_CAPABILITIES if capability in content]

    assert forbidden == [], f"{workflow} bypasses the Kargo/Argo GitOps deployment boundary: {forbidden}"
