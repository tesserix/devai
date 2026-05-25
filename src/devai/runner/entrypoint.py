"""Entrypoint for the `devai-runner` image.

A single binary running inside the spawned pod. Steps:

  1. Read env (`DEVAI_RUNNER_AGENT`, `DEVAI_RUNNER_TASK_ID`,
     `DEVAI_RUNNER_STAGE`, `DEVAI_REGISTRY_URL`, `DEVAI_STAGE_CONFIG`).
  2. Resolve the agent against aregistry → pull image, skills, prompts,
     MCP servers, allowed_tools. Fallback to local YAML specializations
     when the registry is unreachable.
  3. Build the agent's runtime context (allowed tools, system prompt,
     SCM client, LLM adapter).
  4. Invoke `agent.run(state)`.
  5. Print `RESULT::<json>` on stdout so the JobRunnerStage in the api
     can parse the outcome.

The script is deliberately self-contained — it does NOT import from
`devai.pipeline` or `devai.runtime` to keep the runner image small.
It only needs the agent + adapter modules.

Stages that don't have a custom agent (e.g. `noop`, `cleanup`) read
their `__stage` env var, run a hard-coded handler, and exit cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from typing import Any

logger = logging.getLogger("devai.runner")
logging.basicConfig(
    level=os.environ.get("DEVAI_RUNNER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


RESULT_PREFIX = "RESULT::"


def main() -> int:
    """Sync wrapper so the K8s entrypoint stays simple."""
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        logger.exception("runner: unhandled exception")
        _emit_result({"ok": False, "error": traceback.format_exc(limit=20)})
        return 1


async def _run() -> int:
    task_id = os.environ.get("DEVAI_RUNNER_TASK_ID", "")
    stage = os.environ.get("DEVAI_RUNNER_STAGE", "")
    agent_name = os.environ.get("DEVAI_RUNNER_AGENT", stage)
    blueprint = os.environ.get("DEVAI_RUNNER_BLUEPRINT", "")
    repo = os.environ.get("DEVAI_RUNNER_REPO", "")
    intent = os.environ.get("DEVAI_RUNNER_INTENT", "")
    stage_config = _decode_stage_config()

    if not task_id or not stage:
        _emit_result({"ok": False, "error": "missing DEVAI_RUNNER_TASK_ID or DEVAI_RUNNER_STAGE"})
        return 2

    logger.info(
        "runner: task=%s stage=%s agent=%s repo=%s blueprint=%s",
        task_id, stage, agent_name, repo, blueprint,
    )

    # Build the minimum config + adapters the agent needs.
    config = _load_settings()
    agent_meta = await _resolve_agent(agent_name, config)

    # Stage handlers fall into a few buckets. Most run a Specialization
    # (YAML) or a legacy Python agent. A small set of stages have
    # special semantics (scaffold a real Next.js app, spin a preview
    # pod, …); they map by stage name.
    handler = _STAGE_HANDLERS.get(stage)
    if handler is None:
        handler = _run_agent

    try:
        result = await handler(
            agent_name=agent_name,
            stage=stage,
            task_id=task_id,
            repo=repo,
            intent=intent,
            blueprint=blueprint,
            agent_meta=agent_meta,
            stage_config=stage_config,
            config=config,
        )
    except Exception:  # noqa: BLE001
        logger.exception("runner: handler %s raised", stage)
        _emit_result({"ok": False, "error": traceback.format_exc(limit=20)})
        return 1

    if not isinstance(result, dict):
        result = {"value": result}
    result.setdefault("ok", True)
    _emit_result(result)
    return 0 if result.get("ok") else 1


# ─────────────────────────────────────────────────────────────────────
# Default handler — run the agent from aregistry / specializations
# ─────────────────────────────────────────────────────────────────────


async def _run_agent(
    *,
    agent_name: str,
    stage: str,
    task_id: str,
    repo: str,
    intent: str,
    blueprint: str,
    agent_meta: dict[str, Any] | None,
    stage_config: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    """Generic agent runner — invokes a Specialization or legacy agent.

    The execution boils down to:

        from devai.specializations.service import SpecializationService
        service = SpecializationService(config)
        await service.start()
        return await service.invoke(agent_name, ALMState slice)

    We avoid importing SpecializationService at module top-level so this
    file remains importable even in environments where the full devai
    package isn't fully wired (early CI tests, slim images, etc.).
    """
    try:
        from devai.specializations.service import SpecializationService
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"runner: SpecializationService import failed: {e}",
            "stage": stage,
        }

    service = SpecializationService(config)
    try:
        await service.start()
    except Exception:  # noqa: BLE001
        logger.exception("SpecializationService.start failed")

    state_slice = {
        "run_id": task_id,
        "repo_full_name": repo,
        "requirements": intent,
        "stage": stage,
        "blueprint": blueprint,
        **stage_config,
    }

    try:
        patch = await service.invoke(agent_name, state_slice)
    except AttributeError:
        # SpecializationService.invoke may not exist in this build. Fall
        # back to the legacy agent path.
        patch = await _invoke_legacy(agent_name, state_slice, config)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"agent {agent_name} failed: {e}",
            "stage": stage,
        }

    if not isinstance(patch, dict):
        patch = {"value": patch}
    patch.setdefault("ok", True)
    patch.setdefault("stage", stage)
    return patch


async def _invoke_legacy(
    agent_name: str, state: dict[str, Any], config: Any
) -> dict[str, Any]:
    """Construct a legacy `devai.agents.<name>` and call `run(state)`.

    A best-effort path so existing agent classes work in the runner
    without porting them to Specializations first.
    """
    import importlib

    module_name = f"devai.agents.{agent_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"no agent module for {agent_name}: {e}"}

    # Convention: ClassName = <CamelCase agent_name>Agent
    class_name = "".join(p.title() for p in agent_name.split("_")) + "Agent"
    cls = getattr(module, class_name, None)
    if cls is None:
        return {"ok": False, "error": f"{module_name}.{class_name} not found"}

    try:
        # Most agents accept (scm, state_manager, config, event_bus).
        # In the runner we pass None for components the agent can do
        # without; agents that strictly need them will raise.
        agent = cls(None, None, config, None)
        patch = await agent.run(state)
        if not isinstance(patch, dict):
            patch = {"value": patch}
        return patch
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"legacy agent {agent_name} failed: {e}"}


# ─────────────────────────────────────────────────────────────────────
# Special stage handlers (curated templates, scaffold, preview)
# ─────────────────────────────────────────────────────────────────────


async def _stage_scan_repo(*, repo: str, intent: str, stage_config: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Light scan that classifies a repo as blank vs not.

    Mirrors the logic of `/api/scm/repos/<owner>/<name>/scan` but runs
    against the cloned working tree mounted at /devai/work — much
    faster than re-querying the SCM API.
    """
    work = "/devai/work"
    if not os.path.isdir(work):
        return {"ok": True, "is_blank": True, "reason": "no working tree"}

    # Look for marker files. If none exist, treat as blank.
    markers = (
        "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
        "pom.xml", "build.gradle", "Gemfile",
    )
    found = [m for m in markers if os.path.exists(os.path.join(work, m))]
    file_count = 0
    for root, _, files in os.walk(work):
        if ".git" in root:
            continue
        file_count += len(files)

    is_blank = not found and file_count <= 2  # README + LICENSE only
    return {
        "ok": True,
        "is_blank": is_blank,
        "markers_found": found,
        "file_count": file_count,
        "suggested_stack": _suggest_stack(intent) if is_blank else None,
    }


def _suggest_stack(intent: str) -> str:
    """Trivial keyword heuristic. The real picker is the LLM agent —
    this is just the first-pass hint shown to the user."""
    text = (intent or "").lower()
    if any(k in text for k in ("ecommerce", "store", "shop", "marketplace", "saas")):
        return "nextjs"
    if any(k in text for k in ("api", "backend", "microservice")):
        return "go"
    if any(k in text for k in ("dashboard", "spa", "single page")):
        return "vite"
    return "nextjs"


_STAGE_HANDLERS: dict[str, Any] = {
    "scan_repo": _stage_scan_repo,
}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _decode_stage_config() -> dict[str, Any]:
    raw = os.environ.get("DEVAI_STAGE_CONFIG", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _load_settings() -> Any:
    """Build a minimal Settings — the runner only uses LLM + SCM bits.

    Falls back to None if the import fails so handlers can still run
    against env vars."""
    try:
        from devai.config import Settings

        return Settings()  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        logger.exception("Settings construction failed in runner")
        return None


async def _resolve_agent(agent_name: str, config: Any) -> dict[str, Any] | None:
    """Look up the agent in aregistry. Returns None on miss; the runner
    still proceeds with a local-YAML specialization or legacy agent."""
    if not agent_name:
        return None
    try:
        from devai.registry import create_registry_client
    except Exception:  # noqa: BLE001
        return None
    try:
        client = create_registry_client(config)
        agent = await asyncio.to_thread(_safe_get_agent, client, agent_name)
    except Exception:  # noqa: BLE001
        logger.exception("registry: get_agent(%s) failed", agent_name)
        return None
    if agent is None:
        return None
    return {
        "name": getattr(agent, "name", agent_name),
        "image": getattr(agent, "image", None),
        "skills": list(getattr(agent, "skills", []) or []),
        "prompts": list(getattr(agent, "prompts", []) or []),
        "mcp_servers": list(getattr(agent, "mcp_servers", []) or []),
        "model_provider": getattr(agent, "model_provider", None),
        "model_name": getattr(agent, "model_name", None),
    }


def _safe_get_agent(client: Any, name: str) -> Any:
    try:
        return client.get_agent(name)
    except Exception:  # noqa: BLE001
        return None


def _emit_result(payload: dict[str, Any]) -> None:
    """Write the RESULT marker line to stdout. The JobRunnerStage parses
    the last RESULT:: line from the pod logs."""
    try:
        encoded = json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001
        encoded = json.dumps({"ok": False, "error": "result not serialisable"})
    sys.stdout.write(f"\n{RESULT_PREFIX}{encoded}\n")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
