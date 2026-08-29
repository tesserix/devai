from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from devai.adapters.llm.factory import resolve_spec_provider
from devai.adapters.llm.model_policy import provider_serves

ReferenceResolver = Callable[[str, str], Awaitable[dict[str, Any] | bool | None]]

_MAX_REFERENCES_PER_FIELD = 100

_INSTRUCTION_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|override)\b(?:\s+\w+){0,5}\s+\b(?:previous|prior|system|developer)\b"
    r"(?:\s+\w+){0,3}\s+\binstructions?\b",
    re.IGNORECASE,
)
_SECRET_DISCLOSURE = re.compile(
    r"\b(?:reveal|print|return|exfiltrate|leak)\b(?:\s+\w+){0,7}\s+"
    r"\b(?:api[ -]?keys?|secrets?|credentials?|environment variables?)\b",
    re.IGNORECASE,
)
_DISCLOSURE_NEGATION_PREFIX = re.compile(
    r"\b(?:do not|don't|never|must not)(?:\s+\w+){0,2}\s+$",
    re.IGNORECASE,
)


class HarnessStage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["build", "security"]
    status: Literal["passed", "blocked", "approval_required"]
    issues: list[str] = Field(default_factory=list)


class AgentHarnessReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["passed", "blocked", "approval_required"]
    stages: list[HarnessStage]
    issues: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    approved_by: str | None = None
    approval_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def approve(self, approver: str, reason: str) -> AgentHarnessReport:
        if self.status != "approval_required":
            raise ValueError("only approval-required reports can be approved")
        stages = [
            stage.model_copy(update={"status": "passed", "issues": []})
            if stage.status == "approval_required"
            else stage
            for stage in self.stages
        ]
        return self.model_copy(
            update={
                "status": "passed",
                "stages": stages,
                "issues": [],
                "requires_approval": False,
                "approved_by": approver,
                "approval_reason": reason,
            }
        )


class AgentHarness:
    def __init__(self, resolve: ReferenceResolver) -> None:
        self._resolve = resolve

    async def run(self, manifest: dict[str, Any]) -> AgentHarnessReport:
        spec = manifest.get("spec")
        if not isinstance(spec, dict):
            issue = "spec must be an object"
            return AgentHarnessReport(
                status="blocked",
                stages=[
                    HarnessStage(name="build", status="blocked", issues=[issue]),
                    HarnessStage(name="security", status="passed"),
                ],
                issues=[issue],
            )

        build_issues, referenced_prompts = await self._build_issues(spec)
        security_issues = self._security_issues(spec, referenced_prompts)
        risk_level = str(spec.get("riskLevel") or "").strip().lower()
        approval_issues = (
            [f"spec.riskLevel {risk_level} requires audited human approval"]
            if risk_level in {"high", "critical"}
            else []
        )
        blocked = bool(build_issues or security_issues)
        requires_approval = bool(approval_issues)
        status: Literal["passed", "blocked", "approval_required"]
        if blocked:
            status = "blocked"
        elif requires_approval:
            status = "approval_required"
        else:
            status = "passed"
        security_status: Literal["passed", "blocked", "approval_required"]
        if security_issues:
            security_status = "blocked"
        elif requires_approval:
            security_status = "approval_required"
        else:
            security_status = "passed"
        return AgentHarnessReport(
            status=status,
            stages=[
                HarnessStage(name="build", status="blocked" if build_issues else "passed", issues=build_issues),
                HarnessStage(
                    name="security",
                    status=security_status,
                    issues=[*security_issues, *approval_issues],
                ),
            ],
            issues=[*build_issues, *security_issues, *approval_issues],
            requires_approval=requires_approval,
        )

    async def _build_issues(self, spec: dict[str, Any]) -> tuple[list[str], list[tuple[str, str]]]:
        issues = self._reference_shape_issues(spec)
        referenced_prompts: list[tuple[str, str]] = []
        references: list[tuple[str, str, str, str]] = []
        self._append_reference(references, "skills", "spec.skill", "skill", spec.get("skill"))
        self._append_references(references, "skills", "spec.skills", "skill", spec.get("skills"))
        self._append_reference(references, "prompts", "spec.promptRef", "prompt", spec.get("promptRef"))
        self._append_references(references, "prompts", "spec.prompts", "prompt", spec.get("prompts"))
        self._append_references(references, "tools", "spec.tools", "tool", spec.get("tools"))
        self._append_references(
            references,
            "mcp-servers",
            "spec.mcpServers",
            "MCP server",
            spec.get("mcpServers"),
        )

        seen: set[tuple[str, str]] = set()
        for plural, path, kind, name in references:
            key = (plural, name)
            if key in seen:
                continue
            seen.add(key)
            resolved = await self._resolve(plural, name)
            if resolved is None or resolved is False:
                issues.append(f"{path} references an unavailable {kind}: {name}")
            elif plural == "prompts" and path == "spec.prompts" and isinstance(resolved, dict):
                resolved_spec = resolved.get("spec")
                if isinstance(resolved_spec, dict):
                    for field in ("systemPrompt", "userPromptTemplate", "template"):
                        content = resolved_spec.get(field)
                        if isinstance(content, str) and content.strip():
                            referenced_prompts.append((f"{path}[{name}].{field}", content))
        model_issue = self._model_issue(spec)
        if model_issue:
            issues.append(model_issue)
        return issues, referenced_prompts

    @staticmethod
    def _reference_shape_issues(spec: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        for path, value in (("spec.skill", spec.get("skill")), ("spec.promptRef", spec.get("promptRef"))):
            if value is not None and not isinstance(value, str):
                issues.append(f"{path} must be a reference name")
        for path, value in (
            ("spec.skills", spec.get("skills")),
            ("spec.prompts", spec.get("prompts")),
            ("spec.tools", spec.get("tools")),
            ("spec.mcpServers", spec.get("mcpServers")),
        ):
            if value is None:
                continue
            if not isinstance(value, list):
                issues.append(f"{path} must be an array of non-empty reference names")
                continue
            if len(value) > _MAX_REFERENCES_PER_FIELD:
                issues.append(f"{path} must not contain more than {_MAX_REFERENCES_PER_FIELD} references")
                continue
            if any(not isinstance(item, str) or not item.strip() for item in value):
                issues.append(f"{path} must contain only non-empty reference names")
        return issues

    @staticmethod
    def _model_issue(spec: dict[str, Any]) -> str:
        model = spec.get("model")
        if not isinstance(model, dict):
            return ""
        provider = str(model.get("provider") or "").strip().lower()
        model_name = str(model.get("name") or "").strip()
        backend = resolve_spec_provider(provider)
        if backend and model_name and not provider_serves(backend, model_name):
            return f"spec.model provider {provider} cannot serve model {model_name}"
        return ""

    @staticmethod
    def _append_reference(
        out: list[tuple[str, str, str, str]],
        plural: str,
        path: str,
        kind: str,
        value: Any,
    ) -> None:
        if isinstance(value, str) and value.strip():
            out.append((plural, path, kind, value.strip()))

    @staticmethod
    def _append_references(
        out: list[tuple[str, str, str, str]],
        plural: str,
        path: str,
        kind: str,
        value: Any,
    ) -> None:
        if not isinstance(value, list) or len(value) > _MAX_REFERENCES_PER_FIELD:
            return
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append((plural, path, kind, item.strip()))

    @staticmethod
    def _security_issues(spec: dict[str, Any], referenced_prompts: list[tuple[str, str]]) -> list[str]:
        issues: list[str] = []
        for path, value in (("spec.tools", spec.get("tools")), ("spec.mcpServers", spec.get("mcpServers"))):
            if isinstance(value, list) and any(isinstance(item, str) and "*" in item for item in value):
                issues.append(f"{path} must not contain wildcard grants")

        system_prompt = spec.get("systemPrompt")
        if isinstance(system_prompt, str):
            issues.extend(AgentHarness._prompt_security_issues("spec.systemPrompt", system_prompt))
        for path, content in referenced_prompts:
            issues.extend(AgentHarness._prompt_security_issues(path, content))
        return issues

    @staticmethod
    def _prompt_security_issues(path: str, content: str) -> list[str]:
        issues: list[str] = []
        if _INSTRUCTION_OVERRIDE.search(content):
            issues.append(f"{path} contains an instruction-override pattern")
        if AgentHarness._requests_secret_disclosure(content):
            issues.append(f"{path} requests disclosure of secrets or credentials")
        return issues

    @staticmethod
    def _requests_secret_disclosure(system_prompt: str) -> bool:
        for match in _SECRET_DISCLOSURE.finditer(system_prompt):
            prefix = system_prompt[max(0, match.start() - 40) : match.start()]
            if not _DISCLOSURE_NEGATION_PREFIX.search(prefix):
                return True
        return False


__all__ = ["AgentHarness", "AgentHarnessReport", "HarnessStage"]
