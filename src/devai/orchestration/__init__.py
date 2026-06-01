"""Durable orchestration — the generic Temporal backbone for DevAI.

Everything here is **blueprint-agnostic**: a single :class:`BlueprintWorkflow`
interprets *any* blueprint DAG and runs each stage as one generic activity. There
is no per-blueprint or per-agent workflow code — add a YAML blueprint and it runs
durably with zero changes here.

Modules:
  * ``serde``      — dataclass ↔ plain-dict conversion for Temporal payloads
  * ``context``    — worker-global StageDeps/registry the activity executes against
  * ``activities`` — the one generic ``run_stage`` activity
  * ``workflows``  — the one generic ``BlueprintWorkflow``
  * ``worker``     — registers the workflow + activity and polls a task queue
"""

from __future__ import annotations
