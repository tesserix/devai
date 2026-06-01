"""SRE Studio — author, dry-run, and publish SRE configs from DevAI.

The studio is the authoring surface that lives in the DevAI (ALM) app:
operators compose custom SRE blueprints and agents, save them as drafts,
dry-run them (live but with every side effect suppressed), and publish
them to the shared agentic-registry. The SRE runtime then consumes and
schedules the published artifacts — DevAI authors, SRE runs.

  draft (Postgres sre_config_drafts)
    → dry-run (PipelineService, dry_run=True — no incidents/PRs/pages)
    → publish (AuthoringService → hot-register + agentic-registry)
        → SRE runtime consumes + schedules
"""

from devai.sre_studio.service import SREStudioError, SREStudioService

__all__ = ["SREStudioService", "SREStudioError"]
