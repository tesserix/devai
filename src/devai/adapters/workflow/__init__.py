"""Workflow (durable orchestration) adapter family.

Abstracts *how* a blueprint is executed — in-process or durably on Temporal —
behind one ``run_blueprint(blueprint, task)`` call. Blueprint- and agent-agnostic
by construction: one adapter runs any DAG, so new blueprints/agents need no change
here. Selection is one env var, ``DEVAI_WORKFLOW_PROVIDER``.

Canonical family shape (per CLAUDE.md): base ABC + factory + noop + one file per
backend, with lazy SDK imports.
"""

from __future__ import annotations

from .base import (
    AdapterNotConfigured,
    AdapterNotInstalled,
    WorkflowAdapter,
    WorkflowAdapterError,
)
from .factory import KNOWN_PROVIDERS, create_workflow_adapter
from .inproc import InProcWorkflowAdapter
from .noop import NoopWorkflowAdapter

__all__ = [
    "KNOWN_PROVIDERS",
    "AdapterNotConfigured",
    "AdapterNotInstalled",
    "InProcWorkflowAdapter",
    "NoopWorkflowAdapter",
    "WorkflowAdapter",
    "WorkflowAdapterError",
    "create_workflow_adapter",
]
