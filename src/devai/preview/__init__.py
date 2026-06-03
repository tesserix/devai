"""Live preview service — on-demand ephemeral preview environments.

A thin orchestration layer over the existing preview engine
(``devai.runtime.job_spec.build_preview_manifests`` +
``K8sJobRuntime.apply_preview``) so the dashboard can start/stop a running,
hot-reloading preview for any repo on demand — independent of a full
pipeline run. Sessions are persisted in ``preview_sessions``; routing +
auth go through devai-auth-bff (preview-<id>.tesserix.app).
"""

from devai.preview.service import PreviewError, PreviewService

__all__ = ["PreviewService", "PreviewError"]
