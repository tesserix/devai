"""AgentRunner — runs a YAML specialization as an autonomous tool-using agent.

This is the runtime the `_yaml_only_stub` in
`devai.pipeline.stages.specialization` was a placeholder for. Given a
resolved `Specialization` and the current `DevAITask`, it:

  1. builds a system prompt from the spec and a user prompt that hydrates
     the `context_keys` the spec expects out of `task.agent_context`;
  2. binds the spec's `allowed_tools` to concrete handlers via
     `devai.tools.registry.bind` (the spec's tool list becomes a real gate);
  3. runs a bounded tool-calling loop on `deps.llm.generate` (≤ max_turns),
     threading assistant tool calls and tool results back each round;
  4. extracts a final JSON handover, validates it against
     `spec.handover_schema`, and returns it as the patch the stage writes
     under `spec.output_key`.

It degrades gracefully: no LLM adapter wired → a clear stub patch (so a
blueprint referencing a yaml-only role doesn't crash a pod that has no
model configured). A tool that errors returns its error text to the model
rather than killing the loop.

The loop deliberately uses the native `LLMAdapter` (not LangChain) so it
works with every provider DevAI's adapter layer supports and stays free of
the chat agent's vendor lock-in.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole
from devai.specializations.base import AgentRuntime
from devai.tools import registry as tool_registry

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.types import DevAITask
    from devai.specializations.base import Specialization

logger = logging.getLogger(__name__)

# Hard ceiling independent of the spec, so a misconfigured `max_turns: 9999`
# can't run a pod forever.
_MAX_TURNS_CEILING = 60


@dataclass(slots=True)
class AgentRunResult:
    """What a single agent run produced."""

    patch: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    turns: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    trace_steps: list[dict[str, Any]] = field(default_factory=list)
    stub: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "stub": self.stub,
            "error": self.error,
        }


class AgentRunner:
    """Executes one specialization against the LLM with its allowed tools."""

    def __init__(self, deps: StageDeps) -> None:
        self.deps = deps

    # ── Public API ────────────────────────────────────────────────────

    async def run(
        self,
        spec: Specialization,
        task: DevAITask,
        *,
        extra_context: dict[str, Any] | None = None,
        instruction: str = "",
        llm: LLMAdapter | None = None,
        system_suffix: str = "",
    ) -> AgentRunResult:
        """Run `spec` for `task`. Returns the handover patch + run metadata.

        `extra_context` is merged on top of the task's agent_context when
        building the prompt (a crew lead uses this to pass a member its
        subtask). `instruction` overrides the default "do your job" line
        (a crew lead uses this to give a member a specific assignment).
        `llm` overrides the adapter (SpecAgent passes a role-provider-pinned
        one); `system_suffix` is appended to the system prompt (SpecAgent
        passes the skill-profile guidance) — both default to the plain
        `deps.llm` + bare system prompt for direct callers (e.g. a crew).
        """
        llm = llm if llm is not None else self.deps.llm
        if llm is None:
            logger.info("AgentRunner: no LLM adapter wired — returning stub for %s", spec.name)
            return AgentRunResult(
                patch={"stub": True, "reason": "no_llm_adapter", "spec_name": spec.name},
                stub=True,
            )

        # Per-principal SCM: the agent's git tools use the TRIGGERING user's
        # own credentials (their PAT / GitHub App), falling back to the
        # platform client. Resolved here (async) and handed to the context.
        scm = self.deps.scm
        try:
            scm = await self.deps.scm_for_principal(task.triggered_by or "") or self.deps.scm
        except Exception:  # noqa: BLE001
            logger.debug("AgentRunner: per-principal SCM resolution failed — using platform client", exc_info=True)
        ctx = self._build_tool_context(spec, task, scm=scm)
        # One tool execution layer. ToolDispatcher resolves the spec's
        # allowed_tools against the full catalog — SCM / file / shell / web /
        # checkpoint / gitops via the central registry (with this rich context),
        # plus the validation / security / test / memory / SRE families — and
        # gates mutating tools in a dry run. This is the same layer the YAML
        # specialization stage uses, so a YAML role behaves identically whether
        # it runs here or there.
        # Tests / callers may inject a dispatcher via deps.extra; otherwise
        # build the real one with this run's rich tool context.
        dispatcher = (self.deps.extra or {}).get("tool_dispatcher")
        gateway = None
        if dispatcher is None:
            # In a sandbox pod the env carries the pinned tool modes; outside
            # one `from_env` returns None and dispatch is unchanged.
            from devai.sandbox.gateway import ToolGateway
            from devai.tools.dispatch import ToolDispatcher

            gateway = ToolGateway.from_env(os.environ)
            dispatcher = ToolDispatcher(
                scm,
                dry_run=getattr(task, "dry_run", False),
                triggered_by=task.triggered_by or "",
                tool_context=ctx,
                gateway=gateway,
            )
        system = self._build_system_prompt(spec)
        if system_suffix:
            system = f"{system}\n\n{system_suffix}"
        user = self._build_user_prompt(spec, task, extra_context, instruction)
        images = await self._hydrate_images(task)
        if spec.runtime is AgentRuntime.TESSERIX_ADK:
            from devai.agentruntime.tesserix import TesserixSpecRuntime

            try:
                result = await TesserixSpecRuntime(llm=llm, dispatcher=dispatcher).run(
                    spec,
                    task,
                    system_prompt=system,
                    user_prompt=user,
                    images=images,
                )
            except Exception as e:  # noqa: BLE001 — surface a bounded, redacted agent failure
                from devai.services.redact import redact_secrets

                safe_error = redact_secrets(str(e))[:500]
                logger.error("Tesserix ADK run failed for %s: %s", spec.name, safe_error)
                return AgentRunResult(error=f"adk_error: {safe_error}")

            if gateway is not None:
                result.trace_steps.extend(
                    {
                        "kind": "tool",
                        "name": record.tool,
                        "input": record.arguments,
                        "output": record.response,
                        "mode": record.mode.value,
                        "error": record.error or "",
                        "latency_ms": record.latency_ms,
                    }
                    for record in gateway.records
                )
            return result

        tools = dispatcher.build_tool_specs(list(spec.allowed_tools))
        messages: list[LLMMessage] = [LLMMessage(role=LLMRole.USER, content=user, images=images)]
        result = AgentRunResult()
        max_turns = min(max(1, spec.max_turns or 1), _MAX_TURNS_CEILING)
        tool_nudged = False

        for turn in range(max_turns):
            result.turns = turn + 1
            req = LLMRequest(
                system=system,
                messages=messages,
                tools=tools,
                model=spec.llm_model or "",
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
                extra={
                    "agent": spec.name,
                    "run_id": task.id,
                    "triggered_by": task.triggered_by or "",
                },
            )
            try:
                resp = await llm.generate(req)
            except Exception as e:  # noqa: BLE001 — surface a soft failure
                from devai.services.redact import redact_secrets

                safe_error = redact_secrets(str(e))[:500]
                logger.error("AgentRunner: llm.generate failed for %s: %s", spec.name, safe_error)
                result.error = f"llm_error: {safe_error}"
                break

            if resp.finish_reason == "error":
                # Adapters soft-fail (return an empty error response instead of
                # raising); without this the run looks like a clean empty answer.
                from devai.services.redact import redact_secrets

                detail = redact_secrets(str((resp.extra or {}).get("error") or "provider returned an error response"))[
                    :500
                ]
                finish_raw = str((resp.extra or {}).get("finish_raw") or "")
                if finish_raw in ("UNEXPECTED_TOOL_CALL", "MALFORMED_FUNCTION_CALL") and not tool_nudged:
                    # The prompt talked the model into a tool call it can't make
                    # (agents often reference tools their request never declares).
                    # One corrective turn recovers any such agent generically.
                    tool_nudged = True
                    logger.warning("AgentRunner: %s for %s — retrying with a no-tools nudge", finish_raw, spec.name)
                    messages.append(
                        LLMMessage(
                            role=LLMRole.USER,
                            content=(
                                "Your last reply tried to call a tool that is not available in this "
                                "environment. Do not call tools; answer the task directly in text."
                            ),
                        )
                    )
                    continue
                logger.error("AgentRunner: llm.generate errored for %s: %s", spec.name, detail)
                result.error = f"llm_error: {detail}"
                break

            result.prompt_tokens += resp.usage.prompt_tokens
            result.completion_tokens += resp.usage.completion_tokens
            messages.append(LLMMessage(role=LLMRole.ASSISTANT, content=resp.text, tool_calls=list(resp.tool_calls)))

            if not resp.tool_calls:
                result.final_text = resp.text
                break

            for tool_call in resp.tool_calls:
                result.tool_calls += 1
                output = await dispatcher.execute(tool_call.name, dict(tool_call.arguments))
                if gateway is not None and gateway.records:
                    record = gateway.records[-1]
                    result.trace_steps.append(
                        {
                            "kind": "tool",
                            "name": record.tool,
                            "input": record.arguments,
                            "output": record.response,
                            "mode": record.mode.value,
                            "error": record.error or "",
                            "latency_ms": record.latency_ms,
                        }
                    )
                messages.append(
                    LLMMessage(
                        role=LLMRole.TOOL,
                        content=output if isinstance(output, str) else str(output),
                        name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                )
        else:
            result.error = result.error or f"max_turns ({max_turns}) reached"

        result.patch = self._extract_handover(spec, result.final_text)
        return result

    # ── Prompt construction ───────────────────────────────────────────

    def _build_system_prompt(self, spec: Specialization) -> str:
        parts = [spec.system_prompt.strip()] if spec.system_prompt else []
        # Tell the agent how to finish: emit a single JSON object matching
        # the handover schema as the LAST thing it says.
        if spec.handover_schema:
            fields = ", ".join(
                f"{name} ({f.type}{'' if f.required else ', optional'})" for name, f in spec.handover_schema.items()
            )
            parts.append(
                "When you have completed the task, stop calling tools and reply with a single "
                "JSON object (in a ```json code block) containing exactly these fields: "
                f"{fields}. Do not include any other prose after the JSON."
            )
        else:
            parts.append(
                "When you have completed the task, stop calling tools and give a short summary of what you did."
            )
        return "\n\n".join(parts)

    def _build_user_prompt(
        self,
        spec: Specialization,
        task: DevAITask,
        extra_context: dict[str, Any] | None,
        instruction: str,
    ) -> str:
        lines: list[str] = []
        lines.append(f"Repository: {task.repo or '(none)'}")
        if task.branch_name:
            lines.append(f"Working branch: {task.branch_name}")
        lines.append("")
        lines.append("## Task")
        lines.append(instruction.strip() or task.intent.strip() or "(no intent provided)")

        merged_context: dict[str, Any] = dict(task.agent_context or {})
        if extra_context:
            merged_context.update(extra_context)

        # Surface the handover from upstream stages the spec said it needs.
        relevant = [k for k in spec.context_keys if k in merged_context]
        if relevant:
            lines.append("")
            lines.append("## Context from prior stages")
            for key in relevant:
                lines.append(f"### {key}")
                lines.append(_short_json(merged_context[key]))

        return "\n".join(lines)

    # ── Multimodal context ────────────────────────────────────────────

    async def _hydrate_images(self, task: DevAITask) -> list[dict[str, str]]:
        """Fetch composer image attachments from the object store as base64.

        `task.agent_context["attachments"]` is a list of object-store keys
        the composer uploaded. Non-image attachments and fetch failures are
        skipped silently — a missing image must not fail the run.
        """
        store = (self.deps.extra or {}).get("object_store")
        keys = (task.agent_context or {}).get("attachments") or []
        if store is None or not isinstance(keys, list) or not keys:
            return []
        import base64

        out: list[dict[str, str]] = []
        for key in keys[:8]:  # cap — don't blow the context window
            media_type = _guess_media_type(str(key))
            if not media_type.startswith("image/"):
                continue
            try:
                blob = await store.get(str(key))
            except Exception:  # noqa: BLE001
                logger.warning("AgentRunner: could not fetch attachment %s", key)
                continue
            out.append({"media_type": media_type, "data": base64.b64encode(blob).decode("ascii")})
        return out

    # ── Handover extraction ───────────────────────────────────────────

    def _extract_handover(self, spec: Specialization, final_text: str) -> dict[str, Any]:
        """Pull the JSON handover out of the final assistant text.

        Tolerant: a fenced ```json block wins; otherwise the last balanced
        {...} object in the text. On failure, return a soft patch carrying
        the raw text so the run isn't silently lost.
        """
        obj = _parse_json_object(final_text)
        if obj is None:
            # No structured handover — preserve the raw text under `text`. This
            # deliberately does NOT fake a `summary`, so a handover_schema that
            # requires one still flags the violation (strict_handover catches it).
            return {"text": final_text}

        from devai.specializations.validator import validate_handover

        violations = validate_handover(spec, obj)
        if violations:
            logger.warning(
                "AgentRunner: %s handover violations: %s",
                spec.name,
                "; ".join(str(v) for v in violations),
            )
            obj["_handover_violations"] = [str(v) for v in violations]
        return obj

    # ── Tool context ──────────────────────────────────────────────────

    def _build_tool_context(
        self, spec: Specialization, task: DevAITask, *, scm: Any = None
    ) -> tool_registry.ToolContext:
        redis = getattr(self.deps.state_manager, "redis", None)
        return tool_registry.ToolContext(
            repo=task.repo,
            branch=task.branch_name or "",
            run_id=task.id,
            agent_name=spec.name,
            triggered_by=task.triggered_by or "",
            trace_id=task.trace_id or "",
            scm=scm if scm is not None else self.deps.scm,
            redis=redis,
            llm=self.deps.llm,
            web_search=(self.deps.extra or {}).get("web_search"),
            object_store=(self.deps.extra or {}).get("object_store"),
            workdir=(self.deps.extra or {}).get("workdir", ""),
            extra={"principal": dict(task.principal or {})},
        )


# ─── helpers ────────────────────────────────────────────────────────────

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # 1) fenced ```json block (last one wins)
    matches = _JSON_FENCE.findall(text)
    for candidate in reversed(matches):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # 2) last balanced top-level object in the raw text
    candidate = _last_balanced_object(text)
    if candidate:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _last_balanced_object(text: str) -> str | None:
    """Return the last top-level {...} substring with balanced braces."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        i = end
        while i >= 0:
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    return text[i : end + 1]
            i -= 1
        end = text.rfind("}", 0, end)
    return None


_IMAGE_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _guess_media_type(key: str) -> str:
    lowered = key.lower()
    for ext, mt in _IMAGE_EXT.items():
        if lowered.endswith(ext):
            return mt
    return "application/octet-stream"


def _short_json(value: Any, limit: int = 2000) -> str:
    try:
        rendered = json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > limit:
        return rendered[:limit] + "\n… (truncated)"
    return rendered


__all__ = ["AgentRunResult", "AgentRunner"]
