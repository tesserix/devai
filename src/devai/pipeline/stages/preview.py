"""Preview-pod stage: applies the Deployment + Service + VirtualService.

This is what makes the ``app-scaffold`` blueprint's `spin_preview_pod`
stage do real work. The blueprint used to reference a ``preview_spinner``
agent that didn't exist; this module supplies the missing piece.

The stage:
  1. Reads the runtime + repo/branch context from the task.
  2. Picks a stack-appropriate dev-server image (Node 20 by default —
     the scaffold blueprint targets Next.js / Vite apps).
  3. Calls :func:`devai.runtime.job_spec.build_preview_manifests` to
     produce the manifest bundle.
  4. Applies all three via :meth:`K8sJobRuntime.apply_preview`.
  5. Stamps the resulting URLs onto ``task.agent_context`` so the
     post_report stage can render them and the dashboard's Preview tab
     can iframe them.

It runs **inline** (not as a Kubernetes Job). Spinning up a Job just to
call the K8s API would double the latency and double the failure
surface — the orchestrator pod already has cluster credentials, so it
applies directly.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)


# Default dev-server image. The scaffolded app installs into /work via
# the init container's git clone, then `npm run dev` (or whatever the
# blueprint sets) takes over.
_DEFAULT_DEV_IMAGE = "node:20-bookworm-slim"
_DEFAULT_DEV_PORT = 3000
_DEFAULT_DEV_COMMAND: list[str] = ["sh", "-lc", "npm install && npm run dev -- --host 0.0.0.0 --port 3000"]

# The editor-bridge image hosts claude-code in a websocket server that
# the dashboard's Editor tab connects to. ``Coming soon`` in the UI =
# the WebSocket terminal isn't shipped yet; the deployment is still
# valid because the bridge container just sits idle until the dashboard
# connects.
_DEFAULT_BRIDGE_IMAGE = "ghcr.io/tesserix/devai-editor-bridge:main"
_DEFAULT_BRIDGE_PORT = 7681


class PreviewSpinnerStage(PipelineStage):
    """Apply the preview pod manifests for the current task.

    Configuration (read from ``stage.config:`` in the blueprint YAML):

      image            override the dev-server container image
      dev_port         dev-server port (defaults to 3000)
      dev_command      list[str] override for the dev entrypoint
      bridge_image     editor-bridge image
      bridge_port      editor-bridge port (defaults to 7681)
      runner_required  when true, hard-fail if the K8s runtime isn't
                       available. Default true — there's no in-process
                       fallback for "spin up a Pod".
    """

    def __init__(self, deps: StageDeps, config: dict[str, Any]) -> None:
        self.deps = deps
        self.config = config or {}

    def name(self) -> str:
        return "spin_preview_pod"

    async def execute(self, task: DevAITask) -> StageResult:
        runtime = self._runtime()
        if runtime is None:
            if str(self.config.get("runner_required", "true")).lower() == "true":
                raise RuntimeError("spin_preview_pod requires K8s runtime — set DEVAI_K8S_RUNTIME_ENABLED=true")
            return StageResult(
                next_state=TaskState.RUNNING,
                message="K8s runtime not available — preview skipped",
                data={"preview_skipped": True},
            )

        from devai.runtime.job_spec import PreviewInputs, build_preview_manifests

        repo = task.repo or task.agent_context.get("repo", "")
        branch = (
            task.branch_name
            or task.agent_context.get("branch_name")
            or task.agent_context.get("default_branch")
            or "main"
        )

        inputs = PreviewInputs(
            run_id=task.id,
            repo=repo,
            branch=branch,
            image=str(self.config.get("image", _DEFAULT_DEV_IMAGE)),
            dev_command=list(self.config.get("dev_command", _DEFAULT_DEV_COMMAND)),
            dev_port=int(self.config.get("dev_port", _DEFAULT_DEV_PORT)),
            editor_bridge_image=str(self.config.get("bridge_image", _DEFAULT_BRIDGE_IMAGE)),
            editor_bridge_port=int(self.config.get("bridge_port", _DEFAULT_BRIDGE_PORT)),
        )

        manifests = build_preview_manifests(runtime.config, inputs)
        applied = await runtime.apply_preview(manifests)

        preview_url = f"https://{applied['preview_host']}" if applied.get("preview_host") else ""
        editor_url = f"https://{applied['editor_host']}" if applied.get("editor_host") else ""

        # Mirror onto the task itself so the dashboard's Preview tab
        # doesn't have to dig through agent_context.
        task.agent_context["preview_url"] = preview_url
        task.agent_context["editor_url"] = editor_url
        task.agent_context["preview_deployment_name"] = applied["deployment_name"]

        logger.info(
            "spin_preview_pod: applied %s — preview=%s editor=%s",
            applied["deployment_name"],
            preview_url,
            editor_url,
        )

        return StageResult(
            next_state=TaskState.RUNNING,
            message=f"Preview live at {preview_url}",
            data={
                "preview_url": preview_url,
                "editor_url": editor_url,
                "preview_deployment_name": applied["deployment_name"],
                "preview_host": applied.get("preview_host", ""),
                "editor_host": applied.get("editor_host", ""),
            },
        )

    def _runtime(self) -> Any:
        extra = self.deps.extra or {}
        return extra.get("k8s_runtime")


def spin_preview_pod_stage(deps: StageDeps, config: dict[str, Any]) -> PipelineStage:
    """Factory matching the StageRegistry contract."""
    return PreviewSpinnerStage(deps, config)


__all__ = ["PreviewSpinnerStage", "spin_preview_pod_stage"]
