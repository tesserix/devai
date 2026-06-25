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
import re
import sys
import traceback
from typing import Any

logger = logging.getLogger("devai.runner")
logging.basicConfig(
    level=os.environ.get("DEVAI_RUNNER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


RESULT_PREFIX = "RESULT::"

# Legacy agents are imported as `devai.agents.<name>`. The name is env-derived
# (DEVAI_RUNNER_AGENT) so it must be validated before it reaches importlib —
# otherwise a value like `../foo`, `os`, or `a.b` could import an unintended
# module (and trigger its import-time side effects). A legacy agent name is a
# single lowercase identifier segment: letters, digits, underscores, no dots.
_LEGACY_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
    triggered_by = os.environ.get("DEVAI_TRIGGERED_BY", "")
    trace_id = os.environ.get("DEVAI_TRACE_ID", "")
    stage_config = _decode_stage_config()

    if not task_id or not stage:
        _emit_result({"ok": False, "error": "missing DEVAI_RUNNER_TASK_ID or DEVAI_RUNNER_STAGE"})
        return 2

    logger.info(
        "runner: task=%s stage=%s agent=%s repo=%s blueprint=%s triggered_by=%s trace=%s",
        task_id,
        stage,
        agent_name,
        repo,
        blueprint,
        triggered_by or "-",
        trace_id or "-",
    )

    # Build the minimum config + adapters the agent needs.
    config = _load_settings()
    # Prefer the dispatcher-baked profile (DEVAI_AGENT_PROFILE env). The
    # JobRunnerStage already paid the aregistry round-trip; re-fetching
    # would be redundant and breaks if aregistry's network policy denies
    # the runner pod. Fall through to a live aregistry call when the env
    # is empty (older dispatchers, manual `kubectl run` for debugging).
    agent_meta = _decode_agent_profile_from_env()
    if agent_meta is None:
        agent_meta = await _resolve_agent(agent_name, config)
    if agent_meta:
        logger.info(
            "runner: agent profile resolved name=%s model=%s/%s skills=%d prompts=%d mcp_servers=%d",
            agent_meta.get("name"),
            agent_meta.get("model_provider") or "-",
            agent_meta.get("model_name") or "-",
            len(agent_meta.get("skills") or []),
            len(agent_meta.get("prompts") or []),
            len(agent_meta.get("mcp_servers") or []),
        )

    # Stage handlers fall into a few buckets. Most run a Specialization
    # (YAML) or a legacy Python agent. A small set have special semantics
    # (scan a repo, scaffold a real app, spin a preview pod, …). These are
    # keyed by the *agent* name (e.g. scan_repo) since one stage type
    # (run_as_job) hosts many such agents; fall back to the stage name for
    # handlers that are genuinely stage-scoped.
    handler = _STAGE_HANDLERS.get(agent_name) or _STAGE_HANDLERS.get(stage)
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
            triggered_by=triggered_by,
            trace_id=trace_id,
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
    triggered_by: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """Generic agent runner — invokes a Specialization or legacy agent.

    The execution boils down to:

        from devai.specializations.service import SpecializationService
        service = SpecializationService(config, registry_client=...)
        await service.start()
        return await service.invoke(agent_name, ALMState slice)

    The aregistry profile (``agent_meta``) is folded into the state
    slice so the specialization sees the canonical skills / prompts /
    MCP server list / model override, instead of just whatever local
    YAML defaults the runner image was built with.
    """
    try:
        from devai.specializations.service import SpecializationService
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"runner: SpecializationService import failed: {e}",
            "stage": stage,
        }

    # The runner constructs its own registry client so the
    # SpecializationService can still hit aregistry for skill / prompt
    # bodies (the env-baked profile only carries names). Construction is
    # cheap and the client is bounded by a 30 s TTL cache.
    registry_client = None
    try:
        from devai.registry import create_registry_client

        registry_client = create_registry_client(config)
    except Exception:
        logger.debug("runner: registry client unavailable — local YAML only", exc_info=True)

    service = SpecializationService(config, registry_client=registry_client)
    try:
        await service.start()
    except Exception:  # noqa: BLE001
        logger.exception("SpecializationService.start failed")

    state_slice: dict[str, Any] = {
        "run_id": task_id,
        "repo_full_name": repo,
        "requirements": intent,
        "stage": stage,
        "blueprint": blueprint,
        # Identity rides with the state so the agent's A2A messages and
        # SCM commits carry the originating user.
        "trigger_actor": triggered_by,
        "trace_id": trace_id,
        **stage_config,
    }

    # Fold the aregistry profile into the state. The specialization
    # reads these keys to override its built-in defaults — registry
    # is authority for skills / prompts / MCP servers / model choice.
    if agent_meta:
        state_slice.setdefault("agent_profile", agent_meta)
        if agent_meta.get("model_provider"):
            state_slice.setdefault("model_provider", agent_meta["model_provider"])
        if agent_meta.get("model_name"):
            state_slice.setdefault("model_name", agent_meta["model_name"])
        if agent_meta.get("skills"):
            state_slice.setdefault("allowed_skills", list(agent_meta["skills"]))
        if agent_meta.get("prompts"):
            state_slice.setdefault("allowed_prompts", list(agent_meta["prompts"]))
        if agent_meta.get("mcp_servers"):
            # Resolve MCP server names → endpoint URLs, honoring the
            # agentgateway routing override when set.
            state_slice["mcp_endpoints"] = _resolve_mcp_endpoints(agent_meta["mcp_servers"], registry_client, config)

    try:
        patch = await service.invoke(agent_name, state_slice)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"agent {agent_name} failed: {e}",
            "stage": stage,
        }
    if patch is None:
        # invoke() runs YAML specs via the Agent SDK; it returns None for an
        # unknown agent or a legacy Python class. Construct that class directly.
        patch = await _invoke_legacy(agent_name, state_slice, config)

    if not isinstance(patch, dict):
        patch = {"value": patch}
    patch.setdefault("ok", True)
    patch.setdefault("stage", stage)
    return patch


async def _invoke_legacy(agent_name: str, state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Construct a legacy `devai.agents.<name>` and call `run(state)`.

    A best-effort path so existing agent classes work in the runner
    without porting them to Specializations first.
    """
    import importlib

    # Validate the env-derived name BEFORE importlib so a crafted value can't
    # import an arbitrary submodule (or its import-time side effects). Only a
    # plain lowercase identifier segment is allowed — no dots, no traversal.
    if not _LEGACY_AGENT_NAME_RE.match(agent_name or ""):
        return {"ok": False, "error": f"invalid legacy agent name: {agent_name!r}"}

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
        "package.json",
        "pyproject.toml",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "Gemfile",
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


async def _stage_pick_stack(*, intent: str, stage_config: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Pick a curated stack for a blank repo from the user's intent.

    Deterministic first-pass (the heavy customisation is the scaffold agent's
    job). Honors an explicit, non-default `stack` in the stage config when the
    user pre-chose one; otherwise derives it from the prompt. The chosen stack
    is lifted into the handover bag so `scaffold_app` / `install_deps` read it.
    """
    chosen = (stage_config.get("stack") or "").strip()
    if not chosen or chosen == "default":
        chosen = _suggest_stack(intent)
    return {"ok": True, "stack": chosen, "reason": f"selected {chosen} for: {intent[:80]}"}


# Marker file → install command. First match wins.
_INSTALL_CMDS: list[tuple[str, list[str]]] = [
    ("package.json", ["npm", "install", "--no-audit", "--no-fund"]),
    ("go.mod", ["go", "mod", "download"]),
    ("requirements.txt", ["pip", "install", "-r", "requirements.txt"]),
    ("pyproject.toml", ["pip", "install", "-e", "."]),
]


async def _stage_install_deps(*, stage_config: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Best-effort dependency pre-install in the cloned working tree.

    Detects the toolchain from marker files and runs the matching install so the
    preview pod's dev server boots fast. Degrades gracefully — a missing tool in
    the base runner image (e.g. no `npm`) returns ok with `deferred=True`; the
    stack-specific preview pod installs at boot. Only a real install FAILURE
    (non-zero exit / timeout) fails the stage.
    """
    work = "/devai/work"
    if not os.path.isdir(work):
        return {"ok": True, "installed": False, "reason": "no working tree"}
    for marker, cmd in _INSTALL_CMDS:
        if not os.path.exists(os.path.join(work, marker)):
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=work,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=480)
            tail = (out or b"").decode("utf-8", "replace")[-2000:]
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "installed": False,
                    "marker": marker,
                    "error": f"{cmd[0]} exited {proc.returncode}",
                    "log_tail": tail,
                }
            return {"ok": True, "installed": True, "marker": marker, "tool": cmd[0], "log_tail": tail}
        except TimeoutError:
            return {"ok": False, "installed": False, "marker": marker, "error": f"{cmd[0]} install timed out"}
        except FileNotFoundError:
            # Toolchain not baked into the base runner — the stack-specific
            # preview pod installs at boot. Don't fail the run.
            return {
                "ok": True,
                "installed": False,
                "deferred": True,
                "marker": marker,
                "reason": f"{cmd[0]} not in runner image — deferred to preview pod",
            }
    return {"ok": True, "installed": False, "reason": "no recognised dependency manifest"}


_STAGE_HANDLERS: dict[str, Any] = {
    "scan_repo": _stage_scan_repo,
    "stack_picker": _stage_pick_stack,
    "dep_installer": _stage_install_deps,
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


def _decode_agent_profile_from_env() -> dict[str, Any] | None:
    """Read the dispatcher-baked aregistry profile out of the Job env.

    The JobRunnerStage JSON-encodes the profile into DEVAI_AGENT_PROFILE
    so the runner doesn't have to re-query aregistry on boot. Returns
    None when the env is empty (back-compat with older dispatchers) so
    the caller can fall back to a live registry lookup.
    """
    raw = os.environ.get("DEVAI_AGENT_PROFILE", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("runner: DEVAI_AGENT_PROFILE not valid JSON — ignoring")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_mcp_endpoints(
    mcp_server_names: list[Any],
    registry_client: Any,
    config: Any,
) -> list[dict[str, str]]:
    """Resolve each MCP server name to an endpoint the agent can dial.

    For each name we ask aregistry for the canonical record (carrying
    its in-cluster URL). When ``DEVAI_AGENTGATEWAY_URL`` is set, we
    rewrite the destination so traffic flows through agentgateway
    (solo.io) instead of the MCP server's Service IP directly. That
    hands traffic policy + tracing to the gateway, which is the whole
    point of having it in the topology.

    Defensive — a missing server name produces a record with
    ``endpoint`` set to the empty string rather than raising. The
    agent decides what to do with an unreachable MCP server.
    """
    if not mcp_server_names:
        return []

    gateway_url = os.environ.get("DEVAI_AGENTGATEWAY_URL", "") or (
        getattr(config, "agentgateway_url", "") or "" if config else ""
    )
    gateway_url = gateway_url.rstrip("/")

    resolved: list[dict[str, str]] = []
    for raw_name in mcp_server_names:
        name = str(raw_name).strip()
        if not name:
            continue

        direct_endpoint = ""
        record_type = ""
        if registry_client is not None:
            try:
                rec = registry_client.get_mcp_server(name)
            except Exception:  # noqa: BLE001
                rec = None
            if rec is not None:
                direct_endpoint = (getattr(rec, "url", "") or "").rstrip("/")
                record_type = getattr(rec, "type", "") or ""

        # Route preference:
        #   - agentgateway set  → /mcp/<name> on the gateway
        #   - else direct       → the URL aregistry handed back
        if gateway_url:
            endpoint = f"{gateway_url}/mcp/{name}"
            via = "agentgateway"
        else:
            endpoint = direct_endpoint
            via = "direct"

        resolved.append(
            {
                "name": name,
                "endpoint": endpoint,
                "direct_endpoint": direct_endpoint,
                "type": record_type,
                "routed_via": via,
            }
        )
    return resolved


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
