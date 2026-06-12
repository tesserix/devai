"""Infrastructure Provisioner Agent — deploys to K8s cluster or VM.

Supports two deployment targets based on requirements:
1. **Kubernetes (ArgoCD)**: Generate Helm charts, push to tesserix-k8s, ArgoCD syncs
2. **VM (SSH)**: Generate systemd units, deploy via SSH + rsync

The agent reads the tech stack detection results and deployment requirements
from the document analyzer to decide the target and generate appropriate
infrastructure code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

INFRA_TOOLS = [
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_create_branch",
        "github_commit_file",
        "github_add_comment",
    }
]

K8S_SYSTEM_PROMPT = """You are a Senior Platform Engineer creating Kubernetes deployment manifests.

## Lane Boundaries — STRICT

You ONLY touch infrastructure files. You NEVER edit application source code.

You DO own:
- `Dockerfile`, `.dockerignore`, `docker-compose.yml`
- `helm/`, `chart/`, `Chart.yaml`, `values.yaml`, all Helm template files
- `k8s/`, `manifests/`, `deploy/` Kubernetes YAMLs
- `Makefile` build/deploy targets
- `.gcloudignore`, `cloudbuild.yaml`, `terraform/`
- ArgoCD Application manifests (in tesserix-k8s, not in app repos)

You NEVER touch:
- Application source code under `src/`, `app/`, `lib/`, `internal/`, `cmd/`, `apps/`
- `.github/workflows/` — that's the CI Engineer's territory
- ORM models, migrations, seed scripts — DB Engineer's lane
- Test files — QA Tester's lane

For the Tesserix platform, ALL deployments go through ArgoCD GitOps:
1. Create Helm chart templates in the `tesserix-k8s` repo
2. Define deployment.yaml, service.yaml, configmap.yaml, externalsecret.yaml
3. Create the ArgoCD Application manifest
4. Follow existing patterns in tesserix-k8s/charts/apps/

Helm chart structure:
```
charts/apps/<service-name>/
├── Chart.yaml
├── values.yaml
├── values-prod.yaml
└── templates/
    ├── deployment.yaml
    ├── service.yaml
    ├── configmap.yaml
    ├── externalsecret.yaml
    ├── serviceaccount.yaml
    └── virtualservice.yaml (if Istio)
```

ArgoCD app:
```
argocd/prod/apps/<project>/<service-name>.yaml
```

Rules:
- NEVER use kubectl apply directly
- All changes must be committed to the tesserix-k8s repo
- Use Workload Identity for GCP service accounts
- Use External Secrets Operator for secrets from GCP Secret Manager
- Add Istio VirtualService for HTTP routing
- Set resource requests and limits
- Add health checks (readiness + liveness probes)
- Use non-root container user"""

VM_SYSTEM_PROMPT = """You are a Senior DevOps Engineer deploying applications to VMs.

Create deployment scripts for VM-based deployment:
1. Generate a systemd service unit file
2. Create a deploy.sh script that:
   - Builds the application
   - Copies artifacts to the VM via rsync/scp
   - Installs dependencies
   - Sets up the systemd service
   - Configures nginx reverse proxy (if web app)
   - Sets up log rotation
   - Runs health checks
3. Create a rollback.sh for reverting to previous version

The deploy script should:
- Be idempotent (safe to run multiple times)
- Create backups before overwriting
- Validate the deployment after startup
- Set proper file permissions (no world-readable secrets)
- Use environment files for configuration (not hardcoded)"""


class InfraProvisionerAgent(BaseAgent):
    """Generates and deploys infrastructure based on detected requirements."""

    name = "infra_provisioner"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Generate infrastructure code based on deployment target."""
        claude = ClaudeProvider(self.config, model=getattr(self.config, "llm_model_dev_api", None))
        tool_executor = GitHubToolExecutor(self.github)

        repo = state.get("repo_full_name", "")
        deploy_target = state.get("deploy_environment", "kubernetes")
        requirements = state.get("requirements", "")
        stories = state.get("stories", [])

        # Check A2A for tech stack info
        inbox_context = a2a.format_inbox_context()

        # Determine deployment target from various signals
        deploy_target = self._determine_target(state, inbox_context)

        system_prompt = VM_SYSTEM_PROMPT if deploy_target == "vm" else K8S_SYSTEM_PROMPT

        story_refs = "\n".join(f"- {s.get('title', '')}" for s in stories[:10])

        user_message = f"""Repository: {repo}
Deployment Target: {deploy_target}

## Requirements
{requirements[:2000]}

## User Stories
{story_refs}

{inbox_context}

Generate the infrastructure code needed to deploy this application.
Follow the patterns described in the system prompt.
Commit the infrastructure code to the appropriate repository."""

        # Inject skill profile so infra knows the right Dockerfile
        # template (multi-stage for the detected stack), exposed ports,
        # and helm conventions.
        from devai.agents.skills import get_skill_profile

        profile = get_skill_profile(state.get("skill_profile_name"))
        system_prompt += "\n\n" + profile.render_for_infra()

        governance = state.get("governance", "")
        if governance:
            system_prompt += f"\n\n## Repository Governance\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system_prompt,
            user_message=user_message,
            tools=INFRA_TOOLS,
            tool_executor=tool_executor.execute,
            max_iterations=self.config.claude_max_iterations_ops,
        )

        # Notify Release Manager about the infra setup
        a2a.handoff(
            "release_manager",
            "Infrastructure Ready",
            f"Deployment infrastructure generated for {deploy_target}.\n\n{result_text[:300]}...",
            payload={"deploy_target": deploy_target},
        )

        return {
            "deploy_environment": deploy_target,
        }

    def _determine_target(self, state: ALMState, inbox_context: str) -> str:
        """Determine deployment target from state and A2A messages."""
        # Check requirements text
        req = state.get("requirements", "").lower()

        if any(kw in req for kw in ["vm", "virtual machine", "ssh", "bare metal"]):
            return "vm"
        if any(kw in req for kw in ["kubernetes", "k8s", "cluster", "helm", "argocd"]):
            return "kubernetes"
        if any(kw in req for kw in ["serverless", "cloud run", "lambda"]):
            return "serverless"

        # Check A2A messages for tech detector hints
        if "deploy=vm" in inbox_context.lower():
            return "vm"
        if "deploy=kubernetes" in inbox_context.lower():
            return "kubernetes"

        # Default for Tesserix
        return "kubernetes"
