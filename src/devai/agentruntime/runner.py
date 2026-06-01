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
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole
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
    ) -> AgentRunResult:
        """Run `spec` for `task`. Returns the handover patch + run metadata.

        `extra_context` is merged on top of the task's agent_context when
        building the prompt (a crew lead uses this to pass a member its
        subtask). `instruction` overrides the default "do your job" line
        (a crew lead uses this to give a member a specific assignment).
        """
        llm: LLMAdapter | None = self.deps.llm
        if llm is None:
            logger.info("AgentRunner: no LLM adapter wired — returning stub for %s", spec.name)
            return AgentRunResult(
                patch={"stub": True, "reason": "no_llm_adapter", "spec_name": spec.name},
                stub=True,
            )

        ctx = self._build_tool_context(spec, task)
        bound = tool_registry.bind(list(spec.allowed_tools), ctx)
        handlers = {b.spec.name: b.handler for b in bound}
        tools = [b.spec for b in bound]

        system = self._build_system_prompt(spec)
        user = self._build_user_prompt(spec, task, extra_context, instruction)
        images = await self._hydrate_images(task)
        messages: list[LLMMessage] = [LLMMessage(role=LLMRole.USER, content=user, images=images)]

        result = AgentRunResult()
        max_turns = min(max(1, spec.max_turns or 1), _MAX_TURNS_CEILING)

        for turn in range(max_turns):
            result.turns = turn + 1
            req = LLMRequest(
                system=system,
                messages=messages,
                tools=tools,
                model=spec.llm_model or "",
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
            )
            try:
                resp = await llm.generate(req)
            except Exception as e:  # noqa: BLE001 — surface as a soft failure
                logger.exception("AgentRunner: llm.generate failed for %s", spec.name)
                result.error = f"llm_error: {e}"
                break

            result.prompt_tokens += resp.usage.prompt_tokens
            result.completion_tokens += resp.usage.completion_tokens

            # Record the assistant turn (carry tool_calls so the provider can
            # bind the tool_results we append next round — see LLMMessage).
            messages.append(
                LLMMessage(role=LLMRole.ASSISTANT, content=resp.text, tool_calls=list(resp.tool_calls))
            )

            if not resp.tool_calls:
                result.final_text = resp.text
                break

            for tc in resp.tool_calls:
                result.tool_calls += 1
                handler = handlers.get(tc.name)
                if handler is None:
                    output = f"ERROR: tool {tc.name!r} is not available to this agent"
                else:
                    try:
                        output = await handler(dict(tc.arguments))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("AgentRunner: tool %s raised", tc.name)
                        output = f"ERROR: tool {tc.name} failed: {e}"
                messages.append(
                    LLMMessage(
                        role=LLMRole.TOOL,
                        content=output if isinstance(output, str) else str(output),
                        name=tc.name,
                        tool_call_id=tc.id,
                    )
                )
        else:
            # loop exhausted without a tool-free turn
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
                f"{name} ({f.type}{'' if f.required else ', optional'})"
                for name, f in spec.handover_schema.items()
            )
            parts.append(
                "When you have completed the task, stop calling tools and reply with a single "
                "JSON object (in a ```json code block) containing exactly these fields: "
                f"{fields}. Do not include any other prose after the JSON."
            )
        else:
            parts.append(
                "When you have completed the task, stop calling tools and give a short summary "
                "of what you did."
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
            return {"summary": final_text.strip()[:4000], "_no_structured_handover": True}

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

    def _build_tool_context(self, spec: Specialization, task: DevAITask) -> tool_registry.ToolContext:
        redis = getattr(self.deps.state_manager, "redis", None)
        return tool_registry.ToolContext(
            repo=task.repo,
            branch=task.branch_name or "",
            run_id=task.id,
            agent_name=spec.name,
            triggered_by=task.triggered_by or "",
            trace_id=task.trace_id or "",
            scm=self.deps.scm,
            redis=redis,
            llm=self.deps.llm,
            web_search=(self.deps.extra or {}).get("web_search"),
            object_store=(self.deps.extra or {}).get("object_store"),
            workdir=(self.deps.extra or {}).get("workdir", ""),
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
