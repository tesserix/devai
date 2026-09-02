"""Run-an-agent-as-a-K8s-Job stage adapter.

This is the canonical stage handler once the K8s runtime is enabled.
Instead of constructing the agent in-process and calling `agent.run()`,
the handler:

  1. Reads `stage.config.agent` (or falls back to the stage name).
  2. Resolves that agent in the specialization registry → pulls its
     image, allowed_tools, skills, prompts, mcp_servers from aregistry.
  3. Picks the right runner image (per-stack override or the base
     devai-runner).
  4. Builds a V1Job via `runtime.build_job_spec()` and submits it.
  5. Awaits the JobWatcher's completion Event.
  6. Reads the JobOutcome (logs + status), parses the runner's
     `RESULT::` line into StageResult.data, returns to the executor.

The handler is intentionally transparent: from the blueprint YAML's
perspective the only change is the stage key. Existing blueprints that
declare `stage: implement_code` keep working — the executor's wrapper
layer (see executor patch in task 87) routes them through here when
`runner: job` is set.

Failure modes:
  * Runtime missing or not enabled → returns a stub StageResult so the
    pipeline degrades to in-process for that stage (or hard-fails if
    the stage is marked `runner_required: true`).
  * Image not registered for the stage → registers the base runner and
    logs a warning.
  * Job times out → executor's `asyncio.wait_for` handles it; we cancel
    the asyncio.Event so the watcher's late signal doesn't leak.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

if TYPE_CHECKING:
    from devai.runtime import (  # noqa: F401  -- typing only
        JobWatcher,
        K8sJobRuntime,
    )

logger = logging.getLogger(__name__)


# Maps a kagent ModelConfig provider → the Settings attribute holding that
# provider's API key, so a passthrough dispatch forwards the user's matching
# key. Mirrors the connector field keys in settings/models.py.
_KAGENT_PROVIDER_KEY_ATTR: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "groq": "groq_api_key",
    "openrouter": "openrouter_api_key",
    "vertex_gemini": "vertex_api_key",
}

# The Settings attribute holding each provider's selected model — so a per-model
# dispatch can match the user's chosen model to a catalog variant.
_KAGENT_PROVIDER_MODEL_ATTR: dict[str, str] = {
    "anthropic": "claude_model",
    "openai": "openai_model",
    "groq": "groq_model",
    "openrouter": "openrouter_model",
    "vertex_gemini": "vertex_gemini_model",
}


def _a2a_failed(result: dict[str, Any] | None) -> bool:
    """True when an accepted A2A result reports an execution failure.

    A transport-OK dispatch can still carry an LLM failure — kagent sets
    ``status.state=failed`` and/or ``metadata.kagent_error_code`` (bad key, bad
    model, rate limit). The caller must not replay an accepted task because tool
    side effects may already have occurred.
    """
    if not isinstance(result, dict):
        return True
    status = result.get("status")
    if isinstance(status, dict) and status.get("state") == "failed":
        return True
    meta = result.get("metadata")
    return bool(isinstance(meta, dict) and meta.get("kagent_error_code"))


# Stage handlers that genuinely cannot run as a Job (touch state.redis,
# call SCM mid-flight, etc.) can declare `runner: inline` in their YAML.
# This handler honours that hint by returning a stub immediately so the
# wrapper can pick the inline path.
_INLINE_HINT = "inline"

# Authored blueprints can set `stage.config.image` to override the runner
# image. An arbitrary image runs as a K8s Job with the platform's LLM/SCM
# secrets mounted (cluster code-exec via a published blueprint). Restrict the
# override to the trusted devai runner registry prefix; anything else is
# rejected and the stage falls back to the per-stack / default runner image.
# The per-stack image map (RuntimeConfig.runner_image_per_stack) is also
# considered trusted because it is operator-set chart config, not authored.
_TRUSTED_IMAGE_PREFIXES = ("ghcr.io/tesserix/devai/",)


def _image_is_allowed(image: str, runtime: K8sJobRuntime) -> bool:
    """True if ``image`` is on the trusted runner allowlist.

    Allowed sources:
      * any tag under the trusted devai runner registry prefix, OR
      * a value already present in the operator-set per-stack image map, OR
      * the runtime's configured default runner image.
    Everything else (Docker Hub, an attacker-controlled registry, a digest
    pinned elsewhere) is rejected.
    """
    img = (image or "").strip()
    if not img:
        return False
    if img.startswith(_TRUSTED_IMAGE_PREFIXES):
        return True
    per_stack = runtime.config.runner_image_per_stack or {}
    if img in per_stack.values():
        return True
    return img == runtime.config.runner_image


class JobRunnerStage(PipelineStage):
    """Submit an agent run to the cluster and wait for the Job to finish.

    Configuration (read from `stage.config:` in the blueprint YAML):

      agent             specialization key to invoke. Defaults to the
                        stage name (e.g. `implement_code` → spec named
                        `implement_code`).
      image             override the runner image entirely.
      stack             when set, looks up `runner_image_per_stack[stack]`
                        on the runtime config. Defaults to "default".
      runner_required   when true, hard-fail if the K8s runtime isn't
                        available (instead of degrading to a stub).
      next_state        TaskState to transition the task to on success.
                        Same convention as the inline stages.
    """

    def __init__(self, deps: StageDeps, config: dict[str, Any]) -> None:
        self.deps = deps
        self.config = config or {}
        self._stage_name: str = str(self.config.get("__stage_name", "job_runner"))

    # ── PipelineStage surface ───────────────────────────────────────

    def name(self) -> str:
        return self._stage_name

    async def execute(self, task: DevAITask) -> StageResult:
        agent_name = str(self.config.get("agent", self._stage_name))
        from devai.sandbox.job import apply_sandbox_boundary, sandbox_from_stage_config

        sandbox = sandbox_from_stage_config(self.config)
        # Single round-trip to aregistry: pick the image AND capture the
        # full profile so we can pass it through the Job env. Avoids the
        # previous pattern where the dispatcher resolved the image and
        # the runner did a second lookup that it then threw away.
        agent_profile = await self._fetch_agent_profile(agent_name)

        # kagent fast-path: a labelled agent is reached through its Substrate
        # SandboxAgent over A2A instead of spawning an ephemeral Job. kagent is
        # its own control plane, so this is checked BEFORE the Job runtime gate.
        # It remains opt-in. Only failures known to precede acceptance degrade
        # to the Job path; ambiguous or accepted outcomes fail closed.
        if sandbox is None:
            kagent_result = await self._maybe_dispatch_kagent(task, agent_name, agent_profile)
            if kagent_result is not None:
                return kagent_result

        runtime = self._runtime()
        watcher = self._watcher()

        if runtime is None or watcher is None:
            return self._stub_result(task, "K8s runtime not available — stage skipped (will run inline)")

        image = self._resolve_image(runtime, agent_name, agent_profile)
        blueprint = task.blueprint or ""

        from devai.runtime import RunnerJobInputs, build_job_spec

        # Agent control-plane URLs are read off settings so deployments
        # can flip them per environment. Empty = "no gateway, talk direct".
        agentgateway_url = str(getattr(self.deps.config, "agentgateway_url", "") or "")
        kagent_url = str(getattr(self.deps.config, "kagent_url", "") or "")

        inputs = RunnerJobInputs(
            task_id=task.id,
            stage_name=self._stage_name,
            agent_name=agent_name,
            image=image,
            repo=task.repo,
            intent=task.intent,
            blueprint=blueprint,
            extra_env={k: str(v) for k, v in self.config.items() if not k.startswith("__")},
            triggered_by=task.triggered_by or "",
            trace_id=task.trace_id or "",
            agent_profile=agent_profile,
            agentgateway_url=agentgateway_url,
            kagent_url=kagent_url,
        )
        job_spec = build_job_spec(runtime.config, inputs)
        # Same Job, tightened edges — set when an eval dispatches into a sandbox.
        if sandbox is not None:
            job_spec = apply_sandbox_boundary(job_spec, sandbox)
        job_name = job_spec["metadata"]["name"]

        # Register interest BEFORE creating the Job so the watcher can't
        # signal completion before we're listening.
        event = watcher.register(job_name)

        try:
            created_name = await runtime.create_job(job_spec)
        except Exception as e:  # noqa: BLE001 — surface as stage failure
            watcher.consume(job_name)
            raise RuntimeError(f"failed to dispatch job for {self._stage_name}: {e}") from e

        logger.info(
            "stage %s: dispatched job %s for agent=%s",
            self._stage_name,
            created_name,
            agent_name,
        )

        # Await completion. The watcher fires `event` from its k8s watch
        # stream, but a watch can drop the terminal revision (reconnect /
        # re-cycle gaps) — which would otherwise hang us until the stage
        # timeout even though the Job (and the runner's RESULT::) finished. So
        # poll the Job directly every few seconds as a fallback: poll_once
        # reads the Job, and on a terminal status parks the outcome + fires the
        # event itself, independent of the watch stream.
        try:
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=8.0)
                    break  # the watcher fired
                except TimeoutError:
                    if await watcher.poll_once(created_name):
                        break  # poll found it terminal and parked the outcome
        except asyncio.CancelledError:
            # Executor timed out or task was cancelled — best-effort delete
            # so we don't leak the Job. The watcher will still see the
            # final state but no one is listening.
            logger.warning(
                "stage %s: awaiter cancelled — deleting job %s",
                self._stage_name,
                created_name,
            )
            await runtime.delete_job(created_name)
            watcher.consume(created_name)
            raise

        outcome = watcher.consume(created_name)
        if outcome is None:
            raise RuntimeError(f"job {created_name} completed but no outcome was parked — watcher bug")

        if not outcome.succeeded:
            tail = outcome.logs_tail.strip()
            tail = tail[-2048:] if tail else ""
            raise RuntimeError(f"{self._stage_name} failed: {outcome.message}\n--- pod logs ---\n{tail}")

        # Parse the runner's RESULT line — the runner entrypoint writes a
        # JSON blob on a single line prefixed `RESULT::` so we don't need
        # to scrape arbitrary logs.
        data = _parse_runner_result(outcome.logs_tail)
        next_state = self._next_state()
        return StageResult(
            next_state=next_state,
            message=outcome.message,
            data={f"{self._stage_name}_output": data, **_lift_top_level(data)},
        )

    # ── internal helpers ────────────────────────────────────────────

    def _runtime(self) -> K8sJobRuntime | None:
        extra = self.deps.extra or {}
        runtime = extra.get("k8s_runtime")
        return runtime  # may be None when the runtime is disabled

    def _watcher(self) -> JobWatcher | None:
        extra = self.deps.extra or {}
        return extra.get("job_watcher")

    async def _fetch_agent_profile(self, agent_name: str) -> dict[str, Any] | None:
        """Pull the canonical aregistry record for this agent.

        Mandatory gateway mode performs a fresh Registry composition
        resolution and fails before Job or kagent dispatch on any miss. Local
        development retains the legacy best-effort profile lookup.
        """
        registry = (self.deps.extra or {}).get("registry_client") if self.deps.extra else None
        governed = bool(getattr(self.deps.config, "llm_gateway_required", False))
        if governed and not str(getattr(self.deps.config, "llm_gateway_base_url", "") or "").strip():
            raise RuntimeError("governed agent composition unavailable")
        if registry is None:
            if governed:
                raise RuntimeError("governed agent composition unavailable")
            return None
        if governed:
            canonical = f"{agent_name.strip().lower().removesuffix('-agent').replace('_', '-')}-agent"
            try:
                resolution = await asyncio.to_thread(registry.resolve_agent, canonical)
            except Exception as exc:  # noqa: BLE001 -- dependency details stay internal
                logger.warning(
                    "governed Job agent resolution failed agent=%s error_type=%s",
                    canonical,
                    type(exc).__name__,
                )
                raise RuntimeError("governed agent composition unavailable") from None
            if resolution.agent.name != canonical or resolution.unresolved:
                raise RuntimeError("governed agent composition unavailable")
            if (
                resolution.resolved.get("mcpServers")
                and not str(getattr(self.deps.config, "agentgateway_url", "") or "").strip()
            ):
                raise RuntimeError("governed agent composition unavailable")
            agent_meta = resolution.agent
        else:
            try:
                agent_meta = await asyncio.to_thread(registry.get_agent, agent_name)
            except Exception:  # noqa: BLE001
                logger.debug("registry.get_agent(%s) failed", agent_name, exc_info=True)
                return None
        if agent_meta is None:
            if governed:
                raise RuntimeError("governed agent composition unavailable")
            return None
        profile = {
            "name": getattr(agent_meta, "name", agent_name),
            "image": getattr(agent_meta, "image", "") or "",
            "description": getattr(agent_meta, "description", "") or "",
            "version": getattr(agent_meta, "version", "") or "",
            "framework": getattr(agent_meta, "framework", "") or "",
            "language": getattr(agent_meta, "language", "") or "",
            "model_provider": getattr(agent_meta, "model_provider", "") or "",
            "model_name": getattr(agent_meta, "model_name", "") or "",
            "skills": list(getattr(agent_meta, "skills", []) or []),
            "tools": list(getattr(agent_meta, "tools", []) or []),
            "prompts": list(getattr(agent_meta, "prompts", []) or []),
            "mcp_servers": list(getattr(agent_meta, "mcp_servers", []) or []),
            # Registry labels, incl. `devai.io/runtime` — drives kagent routing.
            "labels": dict(getattr(agent_meta, "labels", {}) or {}),
        }
        if governed:
            from devai.registry.composition import snapshot_composition

            composition = snapshot_composition(resolution)
            profile["composition"] = composition
            profile["digest"] = composition["digest"]
        return profile

    # ── kagent A2A dispatch (opt-in Substrate actors) ────────────────

    def _kagent_target(self, agent_name: str, profile: dict[str, Any] | None) -> str | None:
        """Return the kagent agent name to dispatch to, or None for the Job path.

        An agent is kagent-managed when its registry record carries
        ``devai.io/runtime=kagent`` — the same label the kagent-agent-sync
        reconciler selects on. The kagent CR is named after the registry
        record (DNS-1123); we normalise underscores so a stage/agent key like
        ``senior_developer`` maps to the ``senior-developer`` CR.
        """
        from devai.agentic.kagent_client import RUNTIME_KAGENT, RUNTIME_LABEL

        if not profile:
            return None
        labels = profile.get("labels") or {}
        if str(labels.get(RUNTIME_LABEL, "")).strip().lower() != RUNTIME_KAGENT:
            return None
        name = str(profile.get("name") or agent_name).strip().replace("_", "-").lower()
        return name or None

    async def _maybe_dispatch_kagent(
        self,
        task: DevAITask,
        agent_name: str,
        profile: dict[str, Any] | None,
    ) -> StageResult | None:
        """Dispatch to a kagent-managed SandboxAgent, or None to use a Job.

        Returns a StageResult only on a clean kagent dispatch. Configuration
        misses and definite pre-connection rejection fall through to the Job
        path. Once kagent may have accepted a task, errors fail closed because
        replaying it could execute tools twice.
        """
        from devai.agentic.kagent_client import (
            KagentDispatchOutcomeUncertain,
            create_kagent_client,
        )

        # Cheap label check FIRST — the 99% Job path pays no settings cost.
        target = self._kagent_target(agent_name, profile)
        if target is None:
            return None

        # Only now resolve the per-principal Settings overlay, so the kagent
        # switch (and a user/tenant's own controller URL) is honoured DYNAMICALLY
        # — a Settings change lands on the next run with no restart.
        settings = await self._kagent_settings(task)
        if not bool(getattr(settings, "kagent_enabled", True)):
            logger.info(
                "stage %s: agent %s is kagent-labelled but kagent is switched off in Settings — using Job path",
                self._stage_name,
                agent_name,
            )
            return None

        client = create_kagent_client(settings)
        if client is None:
            # Labelled + enabled but no kagent_url wired — fall back to a Job.
            logger.info(
                "stage %s: agent %s is kagent-managed but kagent_url is unset — using Job path",
                self._stage_name,
                agent_name,
            )
            return None

        namespace = str(getattr(settings, "kagent_default_namespace", "") or "") or None
        message = self._kagent_message(task)

        # Shared-key mode (no passthrough): dispatch to the bare agent, no Bearer.
        if not bool(getattr(settings, "kagent_passthrough", False)):
            result = await self._kagent_dispatch_one(client, task, target, message, namespace, "")
            if result is None:
                return None
            if _a2a_failed(result):
                raise KagentDispatchOutcomeUncertain(
                    f"kagent accepted dispatch to {target!r} but reported an execution failure"
                )
            return self._kagent_result(target, result, namespace)

        # Passthrough mode: forward each user's OWN key. Isolation guardrails:
        #   • HUMAN principals only — a real credential is forwarded, so
        #     system/webhook/cron runs never passthrough; they use a Job.
        #   • CONNECTOR-scoped keys only (_kagent_user_key) — never the platform
        #     base key; per-principal, so no user gets another user's key.
        # The key is never logged or persisted (only the Authorization header).
        email = task.triggered_by or ""
        is_human = bool(email) and "@" in email and not email.startswith(("webhook:", "system:"))
        if not is_human:
            logger.info("stage %s: non-human principal — kagent passthrough skipped, using Job", self._stage_name)
            return None

        # Ordered (suffix, provider) chain — the user's chosen (provider, model)
        # first, then their fallback_model, then the rest of the catalog they
        # hold a key for. Each entry maps to a variant `<agent>-<suffix>` on its
        # passthrough ModelConfig. Only a definite pre-connection rejection may
        # try the next target; an accepted failure may already have run tools.
        for suffix, provider in self._kagent_variant_chain(settings):
            key = self._kagent_user_key(settings, provider)
            if not key:
                continue
            variant = f"{target}-{suffix}"
            result = await self._kagent_dispatch_one(client, task, variant, message, namespace, key)
            if result is None:
                continue
            if _a2a_failed(result):
                raise KagentDispatchOutcomeUncertain(
                    f"kagent accepted dispatch to {variant!r} but reported an execution failure"
                )
            return self._kagent_result(variant, result, namespace)

        logger.info("stage %s: no working kagent variant for %s — using Job path", self._stage_name, email)
        return None

    async def _kagent_dispatch_one(
        self, client: Any, task: DevAITask, agent: str, message: str, namespace: str | None, api_key: str
    ) -> dict[str, Any] | None:
        """One A2A dispatch; returns the result, or None on transport error."""
        from devai.agentic.kagent_client import KagentDispatchOutcomeUncertain, KagentDispatchTarget, KagentError

        try:
            result = await client.dispatch(
                agent,
                message,
                namespace=namespace,
                target=KagentDispatchTarget.SANDBOX_AGENT,
                triggered_by=task.triggered_by or "",
                trace_id=task.trace_id or "",
                api_key=api_key,
                request_id=f"{task.id}:{self._stage_name}:{agent}",
                message_id=f"{task.id}:{self._stage_name}:{agent}",
            )
            return result if isinstance(result, dict) else None
        except KagentDispatchOutcomeUncertain:
            logger.error(
                "stage %s: kagent dispatch to %s has an uncertain outcome; refusing Job fallback",
                self._stage_name,
                agent,
            )
            raise
        except KagentError:
            logger.warning("stage %s: kagent dispatch to %s failed", self._stage_name, agent, exc_info=True)
            return None

    def _kagent_result(self, agent: str, result: dict[str, Any], namespace: str | None) -> StageResult:
        from devai.agentic.kagent_client import extract_a2a_text

        logger.info(
            "stage %s: dispatched to kagent agent %s (ns=%s)", self._stage_name, agent, namespace or "kagent-system"
        )
        return StageResult(
            next_state=self._next_state(),
            message=f"{self._stage_name}: kagent agent {agent} completed",
            data={
                f"{self._stage_name}_output": {
                    "runtime": "kagent",
                    "execution_target": "substrate",
                    "agent": agent,
                    "text": extract_a2a_text(result),
                }
            },
        )

    def _kagent_catalog(self) -> list[dict[str, str]]:
        """The (provider, model)→suffix catalog (config kagent_catalog, JSON).
        Falls back to a provider-only catalog from kagent_provider_variants."""
        raw = str(getattr(self.deps.config, "kagent_catalog", "") or "").strip()
        if raw:
            try:
                items = json.loads(raw)
                out = []
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict) and it.get("suffix") and it.get("provider"):
                        out.append(
                            {
                                "suffix": str(it["suffix"]).lower(),
                                "provider": str(it["provider"]).lower(),
                                "model": str(it.get("model", "")),
                            }
                        )
                if out:
                    return out
            except (ValueError, TypeError):
                logger.warning("stage %s: kagent_catalog is not valid JSON — using provider fallback", self._stage_name)
        return [
            {"suffix": p.strip().lower(), "provider": p.strip().lower(), "model": ""}
            for p in str(getattr(self.deps.config, "kagent_provider_variants", "anthropic") or "").split(",")
            if p.strip()
        ]

    def _kagent_variant_chain(self, settings: Any) -> list[tuple[str, str]]:
        """Ordered (suffix, provider) variants to try for this principal:
        the user's chosen (provider, model) first, then their fallback_model,
        then the rest of the catalog. Per-model granularity: the user's
        connector model selects the exact catalog entry; with no model set we
        take the provider's first catalog entry (its default)."""
        catalog = self._kagent_catalog()
        provider = str(getattr(settings, "llm_provider", "") or "").lower()
        model = ""
        if provider in _KAGENT_PROVIDER_MODEL_ATTR:
            model = str(getattr(settings, _KAGENT_PROVIDER_MODEL_ATTR[provider], "") or "")

        chain: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(entry: dict[str, str] | None) -> None:
            if entry and entry["suffix"] not in seen:
                chain.append((entry["suffix"], entry["provider"]))
                seen.add(entry["suffix"])

        # 1. the user's exact (provider, model), else the provider's default entry.
        match = next((e for e in catalog if e["provider"] == provider and model and e["model"] == model), None)
        if match is None:
            match = next((e for e in catalog if e["provider"] == provider), None)
        add(match)
        # 2. the user's fallback_model (any provider), matched by model id.
        fallback = str(getattr(settings, "llm_user_fallback_model", "") or "")
        if fallback:
            add(next((e for e in catalog if e["model"] == fallback), None))
        # 3. the rest of the catalog (fallback) — keys are checked per entry later.
        for e in catalog:
            add(e)
        return chain

    async def _kagent_settings(self, task: DevAITask) -> Any:
        """Resolve the effective kagent settings for this run's principal.

        Returns the per-principal Settings overlay (so the kagent on/off switch
        and a user/tenant's own controller URL/namespace apply), falling back to
        the base config. The full Principal is reconstructed from the task so
        scope resolution covers global → tenant → org → team → user. Never
        raises — degrades to base config.
        """
        principal = None
        try:
            from devai.identity import Principal

            principal = Principal.from_dict(task.principal)
            if principal is None and task.triggered_by and "@" in task.triggered_by:
                principal = Principal(email=task.triggered_by)
        except Exception:  # noqa: BLE001
            principal = None
        if principal is None:
            return self.deps.config
        return await self.deps.settings_overlay(principal)

    def _kagent_user_key(self, settings: Any, provider: str | None = None) -> str:
        """The triggering user's OWN LLM key for ``provider``, or "".

        Only the user's configured connector key is returned — never the
        platform or shared-scope fallthrough. We check the overlay's
        `user_overlaid_attrs` provenance so a tenant, org, team, or global key
        configures the normal Job path but is never forwarded through A2A. If
        the user has no personal connector for ``provider``, that provider is
        skipped.
        """
        provider = (
            provider or str(getattr(self.deps.config, "kagent_model_provider", "anthropic") or "anthropic")
        ).lower()
        attr = _KAGENT_PROVIDER_KEY_ATTR.get(provider, "anthropic_api_key")
        user_overlaid = set(getattr(settings, "user_overlaid_attrs", []) or [])
        if attr not in user_overlaid:
            return ""
        return str(getattr(settings, attr, "") or "")

    def _kagent_message(self, task: DevAITask) -> str:
        """Compose the A2A message text handed to the kagent agent.

        The agent already carries its own prompt/tools from its CR; this just
        states the task. Repo + stage give it the context the Job env would
        otherwise have provided.
        """
        lines = [task.intent or f"Run stage {self._stage_name}"]
        if task.repo:
            lines.append(f"Repo: {task.repo}")
        lines.append(f"Stage: {self._stage_name}")
        return "\n".join(lines)

    def _resolve_image(
        self,
        runtime: K8sJobRuntime,
        agent_name: str,
        profile: dict[str, Any] | None = None,
    ) -> str:
        """Pick the runner image — per-stack override, profile override, or base.

        Authority order: explicit ``stage.config.image`` (1) → per-stack
        runtime image (2) → aregistry agent profile's ``image`` (3) →
        runtime default (4). The aregistry hit was already paid by
        ``_fetch_agent_profile``; this is a pure dict lookup.

        SECURITY (CODE-4): the ``stage.config.image`` override and the
        registry profile's ``image`` are both attacker-influenceable through
        authored blueprints / catalog records. Any image we hand to the Job
        runs in-cluster with LLM + SCM secrets mounted, so every candidate is
        run through ``_image_is_allowed`` (trusted devai registry prefix, the
        operator-set per-stack map, or the default runner image). A rejected
        override does NOT fail the stage — it is logged and we fall through to
        the trusted per-stack / default image so a malicious override can only
        ever downgrade to a safe image, never escalate.
        """
        override = self.config.get("image")
        if override:
            if _image_is_allowed(str(override), runtime):
                return str(override)
            logger.warning(
                "stage %s: rejecting untrusted stage.config.image %r — falling back to trusted runner image",
                self._stage_name,
                override,
            )
        stack = str(self.config.get("stack", "default"))
        per_stack = runtime.config.runner_image_per_stack or {}
        if stack in per_stack:
            return per_stack[stack]

        if profile and profile.get("image"):
            prof_image = str(profile["image"])
            if _image_is_allowed(prof_image, runtime):
                return prof_image
            logger.warning(
                "stage %s: rejecting untrusted agent-profile image %r — falling back to default runner image",
                self._stage_name,
                prof_image,
            )

        return runtime.config.runner_image

    def _next_state(self) -> TaskState | None:
        raw = self.config.get("next_state")
        if not raw:
            return None
        try:
            return TaskState(raw)
        except ValueError:
            logger.warning("stage %s: unknown next_state %r — ignoring", self._stage_name, raw)
            return None

    def _stub_result(self, task: DevAITask, message: str) -> StageResult:
        if self.config.get("runner_required"):
            raise RuntimeError(f"{self._stage_name}: K8s runtime is required but not available")
        logger.info("stage %s: %s", self._stage_name, message)
        return StageResult(
            next_state=None,
            message=message,
            data={f"{self._stage_name}_stub": True},
        )


# ─────────────────────────────────────────────────────────────────────
# Factory + result parsing helpers
# ─────────────────────────────────────────────────────────────────────


def job_runner_stage(deps: StageDeps, config: dict[str, Any]) -> JobRunnerStage:
    """Factory for the stage registry."""
    return JobRunnerStage(deps, config)


_RESULT_PREFIX = "RESULT::"

# Cap the RESULT:: payload we will parse. The runner controls its stdout, so a
# hostile/buggy runner could emit a multi-MB line; bound it before json.loads
# to keep parse cost + the resulting in-memory dict bounded (CODE-13).
_MAX_RESULT_PAYLOAD_BYTES = 128 * 1024  # 128 KiB


def _parse_runner_result(logs: str) -> dict[str, Any]:
    """Find the runner's `RESULT::{...}` line and parse it as JSON.

    Walks the log tail from the end so we pick up the latest result even
    when a runner emitted progress prints. Returns `{}` if no marker, if the
    payload exceeds the size cap, or if it isn't valid JSON.
    """
    if not logs:
        return {}
    for line in reversed(logs.splitlines()):
        line = line.strip()
        if not line.startswith(_RESULT_PREFIX):
            continue
        payload = line[len(_RESULT_PREFIX) :].strip()
        if not payload:
            continue
        if len(payload) > _MAX_RESULT_PAYLOAD_BYTES:
            logger.warning(
                "runner result line exceeds %d bytes (%d) — ignoring",
                _MAX_RESULT_PAYLOAD_BYTES,
                len(payload),
            )
            return {}
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except json.JSONDecodeError:
            logger.warning("runner result line not valid JSON: %s", payload[:200])
            return {}
    return {}


# Per-key allowlist for values promoted from runner-controlled output into the
# handover bag / dashboard. A compromised or buggy runner controls everything
# on its stdout RESULT:: line, so each lifted key is validated by type before
# being merged (CODE-13). URL-shaped keys get an extra host check so a runner
# can't inject an arbitrary iframe src or steer SCM with a spoofed link.
#   "int"   → coerced to int, else dropped
#   "str"   → must be a (length-capped) string
#   "bool"  → must be a real bool
#   "url"   → must be a string whose host is on _ALLOWED_URL_SUFFIXES
_TOP_LEVEL_HANDOVER_SCHEMA: dict[str, str] = {
    "epic_issue_number": "int",
    "pr_number": "int",
    "branch_name": "str",
    "review_decision": "str",
    "security_decision": "str",
    "deploy_status": "str",
    "detected_tech_stack": "str",
    "skill_profile_name": "str",
    "build_status": "str",
    "test_failed": "int",
    "test_passed": "int",
    "test_total": "int",
    "preview_url": "url",
    "editor_url": "url",
}

# Hostnames a runner-supplied URL may point at. Anything else is dropped so a
# compromised runner can't inject a cross-origin iframe src into the dashboard.
_ALLOWED_URL_SUFFIXES = (".tesserix.app",)

# Cap promoted string values so a runner can't bloat task state / the dashboard.
_MAX_LIFTED_STR_LEN = 2048


def _safe_url(value: Any) -> str | None:
    """Return ``value`` if it is an https URL on an allowed host, else None."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) > _MAX_LIFTED_STR_LEN:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except Exception:  # noqa: BLE001
        return None
    if parsed.scheme not in ("https", "http"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if any(host == s.lstrip(".") or host.endswith(s) for s in _ALLOWED_URL_SUFFIXES):
        return value
    return None


def _coerce(kind: str, value: Any) -> Any:
    """Validate/coerce a single lifted value per its schema kind. None drops it."""
    if kind == "int":
        if isinstance(value, bool):  # bool is a subclass of int — reject
            return None
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "str":
        if not isinstance(value, str):
            return None
        return value[:_MAX_LIFTED_STR_LEN]
    if kind == "url":
        return _safe_url(value)
    return None


def _lift_top_level(data: dict[str, Any]) -> dict[str, Any]:
    """Surface well-known scalars from the runner output to the top of
    StageResult.data so other stages and the dashboard can read them
    without nesting through `<stage>_output`.

    Every value is validated against ``_TOP_LEVEL_HANDOVER_SCHEMA`` before it
    is promoted (CODE-13): wrong-typed values are dropped, strings are
    length-capped, and URL fields must resolve to an allowed tesserix host.
    Keys not in the schema are never lifted.
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, kind in _TOP_LEVEL_HANDOVER_SCHEMA.items():
        if key not in data or data[key] is None:
            continue
        coerced = _coerce(kind, data[key])
        if coerced is not None:
            out[key] = coerced
    return out


__all__ = ["JobRunnerStage", "job_runner_stage"]
