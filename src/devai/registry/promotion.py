"""Reusable build, security, evaluation, and ownership gates for Agent publication."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from devai.evaluations.gates import (
    EVAL_APPROVER_ANNOTATION,
    EVAL_BASELINE_ANNOTATION,
    EVAL_COMPARISON_ANNOTATION,
    EVAL_GATE_ANNOTATION_PREFIX,
    EVAL_GATE_LABEL,
    EVAL_GATED_AT_ANNOTATION,
    EVAL_OVERRIDE_REASON_ANNOTATION,
    EVAL_RUN_ANNOTATION,
    EVAL_SUITE_ANNOTATION,
    LIFECYCLE_LABEL,
    AgentGateService,
    AgentPublishGate,
)
from devai.identity import Principal
from devai.registry.agent_harness import AgentHarness, AgentHarnessReport
from devai.registry.client import Prompt, RegistryClient, RegistryError
from devai.registry.semantic import OWNER_LABEL, principal_owner_id

_VISIBILITY_LABEL = "devai.tesserix.app/visibility"
_RUNTIME_LABEL = "devai.io/runtime"
_BUILD_GATE_LABEL = "devai.tesserix.app/build-gate"
_SECURITY_GATE_LABEL = "devai.tesserix.app/security-gate"
_RISK_APPROVER_ANNOTATION = "devai.tesserix.app/risk-approver"
_RISK_APPROVAL_REASON_ANNOTATION = "devai.tesserix.app/risk-approval-reason"
_MODEL_PROVIDERS = frozenset({"anthropic", "claude", "openai", "google", "gemini", "vertex", "vertex_gemini", "groq"})
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


class AgentPromotionError(RuntimeError):
    def __init__(self, status_code: int, detail: str | dict[str, Any]) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PromotionOptions:
    overwrite: bool = False
    allow_gate_override: bool = False
    override_reason: str = ""


class AgentPromotionService:
    def __init__(self, client: RegistryClient, gate_service: AgentGateService | None) -> None:
        self._client = client
        self._gate_service = gate_service

    async def promote(
        self,
        principal: Principal,
        manifest: dict[str, Any],
        *,
        options: PromotionOptions | None = None,
    ) -> dict[str, Any]:
        options = options or PromotionOptions()
        body = deepcopy(manifest)
        owner_id = principal_owner_id(principal)
        if not owner_id:
            raise AgentPromotionError(401, "authenticated principal has no stable subject")

        namespace = str(getattr(self._client, "_namespace", "") or "")
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            raise AgentPromotionError(400, "manifest.metadata is required")
        requested_namespace = str(metadata.get("namespace") or "").strip()
        if namespace and requested_namespace and requested_namespace != namespace:
            raise AgentPromotionError(
                403,
                f"cannot publish into tenant '{requested_namespace}' — this DevAI is scoped to '{namespace}'",
            )
        if namespace:
            metadata["namespace"] = namespace

        labels = metadata.get("labels")
        if labels is None:
            labels = {}
            metadata["labels"] = labels
        if not isinstance(labels, dict):
            raise AgentPromotionError(400, "manifest.metadata.labels must be an object")

        runtime_target = str(labels.get(_RUNTIME_LABEL, "")).strip().lower()
        if runtime_target and runtime_target != "kagent":
            raise AgentPromotionError(
                400,
                {
                    "code": "invalid_agent_runtime",
                    "message": "metadata.labels.devai.io/runtime must be absent or kagent",
                },
            )
        if runtime_target == "kagent":
            raise AgentPromotionError(
                403,
                "user-authored kagent runtime is disabled until the Substrate isolation gate passes",
            )

        contract_errors = _agent_contract_errors(body)
        if contract_errors:
            raise AgentPromotionError(400, {"code": "invalid_agent_manifest", "issues": contract_errors})

        declares_eval_suite = _declares_eval_suite(body)
        for label in (_BUILD_GATE_LABEL, _SECURITY_GATE_LABEL, EVAL_GATE_LABEL, LIFECYCLE_LABEL):
            labels.pop(label, None)
        raw_annotations = metadata.get("annotations")
        annotations = dict(raw_annotations) if isinstance(raw_annotations, dict) else {}
        annotations = {
            str(key): str(value)
            for key, value in annotations.items()
            if key not in {_RISK_APPROVER_ANNOTATION, _RISK_APPROVAL_REASON_ANNOTATION}
            and (
                not str(key).startswith(EVAL_GATE_ANNOTATION_PREFIX)
                or (declares_eval_suite and key == EVAL_RUN_ANNOTATION)
            )
        }
        metadata["annotations"] = annotations
        gate_manifest = deepcopy(body)
        spec = body["spec"]

        prompt_ref = str(spec.get("promptRef") or "").strip()
        if prompt_ref:
            prompt = await self._visible_prompt(principal, prompt_ref)
            if prompt is None:
                raise AgentPromotionError(404, f"prompt not found: {prompt_ref}")
            system_message = _prompt_system_message(prompt)
            if not system_message:
                raise AgentPromotionError(400, f"spec.promptRef '{prompt_ref}' has no non-empty spec.systemPrompt")
            spec["systemPrompt"] = system_message

        harness = await self._run_harness(principal, body)
        if harness.status == "approval_required" and options.allow_gate_override:
            service = self._required_gate_service("agent risk approval gate unavailable")
            try:
                approver = await service.approve_risk(
                    principal,
                    agent_name=str(metadata.get("name") or "").strip(),
                    risk_level=str(spec.get("riskLevel") or "").strip().lower(),
                    reason=options.override_reason,
                )
            except PermissionError as error:
                raise AgentPromotionError(403, str(error)) from error
            except ValueError as error:
                raise AgentPromotionError(422, str(error)) from error
            except RuntimeError as error:
                raise AgentPromotionError(503, str(error)) from error
            harness = harness.approve(approver, options.override_reason.strip())
        if not harness.ok:
            raise AgentPromotionError(
                422,
                {
                    "code": "agent_lifecycle_gate_blocked",
                    "message": "agent build or security gate blocked publication",
                    "gate": harness.model_dump(mode="json", exclude_none=True),
                },
            )

        labels[_BUILD_GATE_LABEL] = "passed"
        labels[_SECURITY_GATE_LABEL] = "passed"
        labels[LIFECYCLE_LABEL] = "published"
        if harness.approved_by:
            annotations[_RISK_APPROVER_ANNOTATION] = harness.approved_by
            annotations[_RISK_APPROVAL_REASON_ANNOTATION] = harness.approval_reason or ""
        metadata["annotations"] = annotations
        labels[OWNER_LABEL] = owner_id
        labels[_VISIBILITY_LABEL] = "private"
        metadata["visibility"] = "private"

        name = str(metadata.get("name") or "").strip()
        if not name:
            raise AgentPromotionError(400, "manifest.metadata.name is required")
        existing = await self._existing(name)
        if existing is not None:
            if _labels(existing).get(OWNER_LABEL) != owner_id:
                raise AgentPromotionError(404, f"artifact not found: {name}")
            if not options.overwrite:
                raise AgentPromotionError(
                    409,
                    (
                        f"'{name}' already exists in tenant '{namespace or 'default'}'. "
                        "Names are unique within a tenant — pick a different name, "
                        "or republish with overwrite to version the existing artifact."
                    ),
                )

        gate: AgentPublishGate | None = None
        if _declares_eval_suite(gate_manifest):
            gate = await self._evaluate_gate(principal, gate_manifest, existing, options)
            _stamp_gate(metadata, labels, gate)

        try:
            result = await asyncio.to_thread(self._client.publish_agent, body)
        except RegistryError as error:
            detail = str(error)
            status = 409 if "409" in detail or "conflict" in detail.lower() else 502
            raise AgentPromotionError(status, detail) from error
        except Exception as error:  # noqa: BLE001 - adapter failure mapped at service boundary
            raise AgentPromotionError(502, f"publish: {error}") from error
        self._client.refresh()
        response = dict(result) if isinstance(result, dict) else {"status": "published"}
        if gate is not None:
            response["gate"] = gate.model_dump(mode="json")
        response["harness"] = harness.model_dump(mode="json", exclude_none=True)
        return response

    async def promote_from_payload(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise AgentPromotionError(422, "agent promotion manifest is required")
        return await self.promote(
            principal,
            manifest,
            options=PromotionOptions(
                overwrite=bool(payload.get("overwrite")),
                allow_gate_override=bool(payload.get("allow_gate_override")),
                override_reason=str(payload.get("override_reason") or ""),
            ),
        )

    async def _existing(self, name: str) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._client.get_artifact_envelope, "agents", name)
        except RegistryError as error:
            raise AgentPromotionError(502, str(error)) from error

    async def _visible_prompt(self, principal: Principal, name: str) -> Prompt | None:
        try:
            prompt = await asyncio.to_thread(self._client.get_prompt, name)
        except RegistryError as error:
            raise AgentPromotionError(502, str(error)) from error
        return prompt if prompt is not None and _visible(prompt, principal) else None

    async def _run_harness(self, principal: Principal, manifest: dict[str, Any]) -> AgentHarnessReport:
        owner_id = principal_owner_id(principal)

        async def resolve(plural: str, name: str) -> dict[str, Any] | None:
            try:
                item = await asyncio.to_thread(self._client.get_artifact_envelope, plural, name)
            except RegistryError as error:
                raise AgentPromotionError(503, "agent lifecycle gate unavailable") from error
            if item is None:
                return None
            owner = _labels(item).get(OWNER_LABEL, "")
            if owner:
                return item if owner == owner_id else None
            return item if _visibility(item) != "private" else None

        return await AgentHarness(resolve).run(manifest)

    async def _evaluate_gate(
        self,
        principal: Principal,
        manifest: dict[str, Any],
        existing: dict[str, Any] | None,
        options: PromotionOptions,
    ) -> AgentPublishGate:
        service = self._required_gate_service("agent evaluation gate unavailable")
        annotations = _annotations(manifest)
        candidate_run_id = str(annotations.get(EVAL_RUN_ANNOTATION) or "").strip()
        if not candidate_run_id:
            raise AgentPromotionError(
                422,
                {"code": "agent_evaluation_run_required", "message": "a declared eval suite requires a run id"},
            )
        baseline_run_id = _annotations(existing or {}).get(EVAL_RUN_ANNOTATION) or None
        if existing is not None and _declares_eval_suite(existing) and baseline_run_id is None:
            raise AgentPromotionError(
                422,
                {
                    "code": "agent_evaluation_baseline_required",
                    "message": "a previously gated agent requires its published evaluation run",
                },
            )
        try:
            gate = await service.evaluate(principal, manifest, candidate_run_id, baseline_run_id=baseline_run_id)
            if gate.status == "blocked" and options.allow_gate_override:
                gate = await service.override(principal, gate, reason=options.override_reason)
        except PermissionError as error:
            raise AgentPromotionError(403, str(error)) from error
        except ValueError as error:
            raise AgentPromotionError(422, str(error)) from error
        except RuntimeError as error:
            raise AgentPromotionError(503, str(error)) from error
        if gate.status == "blocked":
            raise AgentPromotionError(
                422,
                {
                    "code": "agent_evaluation_gate_blocked",
                    "message": "agent evaluation gate blocked publication",
                    "gate": gate.model_dump(mode="json"),
                },
            )
        return gate

    def _required_gate_service(self, message: str) -> AgentGateService:
        if self._gate_service is None:
            raise AgentPromotionError(503, message)
        return self._gate_service


def _agent_contract_errors(body: dict[str, Any]) -> list[str]:
    spec = body.get("spec")
    if not isinstance(spec, dict):
        return ["spec must be an object"]
    errors: list[str] = []
    system_prompt = spec.get("systemPrompt")
    prompt_ref = spec.get("promptRef")
    if system_prompt is not None and not isinstance(system_prompt, str):
        errors.append("spec.systemPrompt must be a string")
    if prompt_ref is not None and not isinstance(prompt_ref, str):
        errors.append("spec.promptRef must be a string")
    has_inline = isinstance(system_prompt, str) and bool(system_prompt.strip())
    has_reference = isinstance(prompt_ref, str) and bool(prompt_ref.strip())
    if not has_inline and not has_reference:
        errors.append("spec.systemPrompt or spec.promptRef is required")
    model = spec.get("model")
    if not isinstance(model, dict):
        errors.append("spec.model must be an object")
    else:
        if not isinstance(model.get("provider"), str) or not model["provider"].strip():
            errors.append("spec.model.provider is required")
        elif model["provider"] not in _MODEL_PROVIDERS:
            errors.append(f"spec.model.provider must be one of {', '.join(sorted(_MODEL_PROVIDERS))}")
        if not isinstance(model.get("name"), str) or not model["name"].strip():
            errors.append("spec.model.name is required")
        temperature = model.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool) or not isinstance(temperature, int | float) or not 0 <= temperature <= 2
        ):
            errors.append("spec.model.temperature must be a number between 0 and 2")
    limits = spec.get("limits")
    if not isinstance(limits, dict):
        errors.append("spec.limits must be an object")
    else:
        max_turns = limits.get("maxTurns")
        if type(max_turns) is not int or not 1 <= max_turns <= 1000:
            errors.append("spec.limits.maxTurns must be an integer between 1 and 1000")
        timeout_seconds = limits.get("timeoutSeconds")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            errors.append("spec.limits.timeoutSeconds must be an integer between 1 and 86400")
    if spec.get("riskLevel") not in _RISK_LEVELS:
        errors.append("spec.riskLevel must be one of low, medium, high, critical")
    eval_suite = spec.get("evalSuite")
    if eval_suite is not None:
        if not isinstance(eval_suite, dict):
            errors.append("spec.evalSuite must be an object")
        else:
            if not isinstance(eval_suite.get("ref"), str) or not eval_suite["ref"].strip():
                errors.append("spec.evalSuite.ref is required")
            if not isinstance(eval_suite.get("version"), str) or not eval_suite["version"].strip():
                errors.append("spec.evalSuite.version is required")
    return errors


def _prompt_system_message(prompt: Prompt) -> str:
    value = prompt.raw.get("systemPrompt") if isinstance(prompt.raw, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _labels(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        metadata = item.get("metadata")
        raw_labels = metadata.get("labels") if isinstance(metadata, dict) else None
    else:
        raw_labels = getattr(item, "raw", {}).get("labels")
        if raw_labels is None:
            raw_labels = getattr(item, "labels", {})
    return {str(key): str(value) for key, value in raw_labels.items()} if isinstance(raw_labels, dict) else {}


def _annotations(item: dict[str, Any]) -> dict[str, str]:
    metadata = item.get("metadata")
    raw = metadata.get("annotations") if isinstance(metadata, dict) else None
    return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}


def _declares_eval_suite(body: dict[str, Any]) -> bool:
    spec = body.get("spec")
    return isinstance(spec, dict) and spec.get("evalSuite") is not None


def _stamp_gate(metadata: dict[str, Any], labels: dict[str, Any], gate: AgentPublishGate) -> None:
    annotations = {
        str(key): str(value)
        for key, value in _annotations({"metadata": metadata}).items()
        if not str(key).startswith(EVAL_GATE_ANNOTATION_PREFIX)
    }
    annotations[EVAL_RUN_ANNOTATION] = gate.candidate_run_id
    annotations[EVAL_SUITE_ANNOTATION] = f"{gate.suite.name}@{gate.suite.version}"
    annotations[EVAL_GATED_AT_ANNOTATION] = gate.evaluated_at
    if gate.baseline_run_id:
        annotations[EVAL_BASELINE_ANNOTATION] = gate.baseline_run_id
    if gate.comparison_id:
        annotations[EVAL_COMPARISON_ANNOTATION] = gate.comparison_id
    if gate.approver:
        annotations[EVAL_APPROVER_ANNOTATION] = gate.approver
    if gate.override_reason:
        annotations[EVAL_OVERRIDE_REASON_ANNOTATION] = gate.override_reason
    metadata["annotations"] = annotations
    labels[EVAL_GATE_LABEL] = gate.status
    labels[LIFECYCLE_LABEL] = "published"


def _visibility(item: Any) -> str:
    if isinstance(item, dict):
        metadata = item.get("metadata")
        value = metadata.get("visibility") if isinstance(metadata, dict) else ""
    else:
        value = getattr(item, "raw", {}).get("visibility", "")
    return str(value or "").strip().lower()


def _visible(item: Any, principal: Principal) -> bool:
    owner = _labels(item).get(OWNER_LABEL, "")
    if owner:
        return owner == principal_owner_id(principal)
    return _visibility(item) != "private"


def editable_agent_manifest(existing: dict[str, Any]) -> dict[str, Any]:
    manifest = deepcopy(existing)
    metadata = manifest.get("metadata")
    if isinstance(metadata, dict):
        labels = metadata.get("labels")
        if isinstance(labels, dict):
            metadata["labels"] = {
                key: value
                for key, value in labels.items()
                if key
                not in {
                    OWNER_LABEL,
                    _VISIBILITY_LABEL,
                    _BUILD_GATE_LABEL,
                    _SECURITY_GATE_LABEL,
                    EVAL_GATE_LABEL,
                    LIFECYCLE_LABEL,
                }
            }
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            metadata["annotations"] = {
                key: value
                for key, value in annotations.items()
                if not str(key).startswith(EVAL_GATE_ANNOTATION_PREFIX)
                and key not in {_RISK_APPROVER_ANNOTATION, _RISK_APPROVAL_REASON_ANNOTATION}
            }
    spec = manifest.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("promptRef"), str) and spec["promptRef"].strip():
        spec["systemPrompt"] = ""
    return manifest


__all__ = ["AgentPromotionError", "AgentPromotionService", "PromotionOptions", "editable_agent_manifest"]
