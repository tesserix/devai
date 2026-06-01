"""SREStudioService — draft lifecycle for SRE configs.

Wraps three existing pieces rather than reinventing them:
  * Database          — persists drafts in ``sre_config_drafts`` (Postgres).
  * PipelineService   — dry-runs a draft blueprint with ``dry_run=True``.
  * AuthoringService  — publishes (validates → hot-registers → pushes to the
                        agentic-registry) on publish.

Validation always goes through the same strict loaders the on-disk YAML
uses, so a draft can never publish something the runtime can't load.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from devai.blueprint.loader import load_blueprint_from_string
from devai.specializations.loader import load_specialization_from_string

if TYPE_CHECKING:
    from devai.authoring.service import AuthoringService
    from devai.services.database import Database

logger = logging.getLogger(__name__)

VALID_KINDS = ("blueprint", "agent")


class SREStudioError(ValueError):
    """Raised on invalid draft input (bad kind, unparseable YAML, etc.)."""


class SREStudioService:
    def __init__(
        self,
        db: Database,
        *,
        pipeline: Any | None = None,
        authoring: AuthoringService | None = None,
    ) -> None:
        self._db = db
        self._pipeline = pipeline
        self._authoring = authoring

    # ── Validation ────────────────────────────────────────────────────

    @staticmethod
    def _validate(kind: str, yaml_text: str) -> str:
        """Validate the YAML for its kind; return the artifact name."""
        if kind == "blueprint":
            try:
                bp = load_blueprint_from_string(yaml_text, source="<sre-studio-draft>")
            except Exception as e:  # noqa: BLE001
                raise SREStudioError(f"invalid blueprint: {e}") from e
            return bp.name
        if kind == "agent":
            try:
                spec = load_specialization_from_string(yaml_text, source="<sre-studio-draft>")
            except Exception as e:  # noqa: BLE001
                raise SREStudioError(f"invalid agent: {e}") from e
            return spec.name
        raise SREStudioError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

    # ── Draft CRUD ────────────────────────────────────────────────────

    async def create_draft(
        self, kind: str, yaml_text: str, created_by: str = "operator", description: str = ""
    ) -> dict[str, Any]:
        name = self._validate(kind, yaml_text)
        draft_id = str(uuid.uuid4())
        await self._db.create_draft(draft_id, kind, name, yaml_text, created_by, description)
        logger.info("sre-studio: created %s draft %s (%s) by %s", kind, name, draft_id, created_by)
        return await self.get_draft(draft_id)

    async def list_drafts(self, status: str | None = None) -> list[dict[str, Any]]:
        return [_public(d) for d in await self._db.list_drafts(status=status)]

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = await self._db.get_draft(draft_id)
        return _public(row) if row else None

    async def update_draft(
        self, draft_id: str, *, yaml_text: str | None = None, name: str | None = None, description: str | None = None
    ) -> dict[str, Any] | None:
        row = await self._db.get_draft(draft_id)
        if row is None:
            return None
        if yaml_text is not None:
            # Re-validate against the draft's kind; keep the artifact name in sync.
            name = self._validate(row["kind"], yaml_text)
        await self._db.update_draft(draft_id, yaml_text=yaml_text, name=name, description=description)
        return await self.get_draft(draft_id)

    async def delete_draft(self, draft_id: str) -> bool:
        return await self._db.delete_draft(draft_id)

    # ── Dry run ───────────────────────────────────────────────────────

    async def dry_run(self, draft_id: str, cluster_id: str = "default") -> dict[str, Any]:
        """Run a draft blueprint for real but with every side effect off.

        Returns a preview: per-stage status/timing plus each role's handover
        output. Persists a compact summary back onto the draft.
        """
        row = await self._db.get_draft(draft_id)
        if row is None:
            raise SREStudioError(f"draft {draft_id!r} not found")
        if row["kind"] != "blueprint":
            raise SREStudioError("dry-run is only supported for blueprint drafts (run a blueprint that uses the agent)")
        if self._pipeline is None:
            raise SREStudioError("pipeline runtime unavailable — cannot dry-run")

        bp = load_blueprint_from_string(row["yaml"], source=f"<dry-run:{draft_id}>")
        # Hot-register so the executor can resolve it by name, then run with
        # dry_run=True (no incidents, PRs, pages, memory writes, or persistence).
        self._pipeline.register_blueprint(bp)
        task = await self._pipeline.run_once(
            intent=f"[dry-run] SRE config {bp.name} (draft {draft_id})",
            blueprint=bp.name,
            trigger_type="dry_run",
            agent_context={"cluster_id": cluster_id, "trigger": "dry_run", "dry_run": True},
            dry_run=True,
        )
        preview = _preview_from_task(task, bp.name)
        await self._db.set_draft_dry_run(draft_id, json.dumps(preview))
        logger.info("sre-studio: dry-ran draft %s (%s) -> %s", draft_id, bp.name, preview.get("state"))
        return preview

    # ── Publish ───────────────────────────────────────────────────────

    async def publish(self, draft_id: str, created_by: str = "operator") -> dict[str, Any]:
        """Publish a draft: validate → hot-register → push to the registry.

        Delegates to AuthoringService so the published artifact follows the
        exact same path as anything else authored in DevAI. Marks the draft
        ``published`` on success.
        """
        row = await self._db.get_draft(draft_id)
        if row is None:
            raise SREStudioError(f"draft {draft_id!r} not found")
        if self._authoring is None:
            raise SREStudioError("authoring service unavailable — cannot publish")

        kind, yaml_text = row["kind"], row["yaml"]
        try:
            if kind == "blueprint":
                result = await self._authoring.create_blueprint(yaml_text, created_by=created_by)
            else:
                result = await self._authoring.create_specialization(yaml_text, created_by=created_by)
        except Exception as e:  # noqa: BLE001 — surface a clean error to the route
            raise SREStudioError(f"publish failed: {e}") from e

        await self._db.set_draft_status(draft_id, "published")
        logger.info("sre-studio: published %s draft %s (%s)", kind, draft_id, result.get("name"))
        return {"published": True, "kind": kind, **result}


# ── Helpers ───────────────────────────────────────────────────────────


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a DB row for the API. dry_run_summary is JSON in the DB."""
    out = dict(row)
    summary = out.get("dry_run_summary")
    if isinstance(summary, str) and summary:
        try:
            out["dry_run_summary"] = json.loads(summary)
        except (ValueError, TypeError):
            pass
    # Timestamps → isoformat strings for JSON.
    for k in ("created_at", "updated_at", "published_at", "last_dry_run_at"):
        v = out.get(k)
        if v is not None and hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


def _preview_from_task(task: Any, blueprint: str) -> dict[str, Any]:
    """Build a dry-run preview from a finished DevAITask."""
    stages: list[dict[str, Any]] = []
    for ev in getattr(task, "stage_events", []) or []:
        phase = ev.phase.value if hasattr(ev.phase, "value") else str(ev.phase)
        stages.append(
            {
                "stage": ev.stage,
                "phase": phase,
                "duration_ms": getattr(ev, "duration_ms", None),
                "message": getattr(ev, "message", ""),
                "error": getattr(ev, "error", None),
            }
        )
    # Each role writes a `<name>_output` key; surface them as the outcome.
    outputs = {
        k: v
        for k, v in (getattr(task, "agent_context", {}) or {}).items()
        if k.endswith("_output") or k in ("correlated_findings",)
    }
    state = task.state.value if hasattr(task.state, "value") else str(task.state)
    return {
        "dry_run": True,
        "blueprint": blueprint,
        "task_id": getattr(task, "id", ""),
        "state": state,
        "stages_completed": list(getattr(task, "stages_completed", []) or []),
        "stages_failed": list(getattr(task, "stages_failed", []) or []),
        "stage_events": stages,
        "outputs": outputs,
        "note": "Dry run — no incidents, remediations, PRs, issues, pages, or memory writes were made.",
    }
