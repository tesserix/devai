from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from devai.sandbox.evals import EvalCase
from devai.sandbox.models import SandboxSpec

Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Version = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
_MAX_DATASET_CASE_BYTES = 1024 * 1024

JudgeDimension = Literal[
    "helpfulness",
    "relevance",
    "reasoning_quality",
    "groundedness",
    "completeness",
]


class EvaluationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactVersionRef(EvaluationModel):
    name: Name
    version: Version


class DatasetCase(EvaluationModel):
    id: Name
    input: Annotated[str, Field(min_length=1, max_length=100_000)]
    expected_output: str | None = None
    expected_regex: str = ""
    expected_json_schema: dict[str, Any] | None = None
    expected_tools: list[Name] = Field(default_factory=list, max_length=100)
    forbidden_tools: list[Name] = Field(default_factory=list, max_length=100)
    expected_tool_arguments: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    tool_order: Literal["ordered", "unordered"] = "ordered"
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_latency_ms: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    context: dict[str, Any] = Field(default_factory=dict)
    tags: list[Name] = Field(default_factory=list, max_length=100)
    human_scores: dict[JudgeDimension, Annotated[float, Field(ge=0, le=1)]] = Field(
        default_factory=dict,
        max_length=5,
    )

    @model_validator(mode="after")
    def arguments_align_with_tools(self) -> DatasetCase:
        if self.expected_tool_arguments and len(self.expected_tool_arguments) != len(self.expected_tools):
            raise ValueError("expected_tool_arguments must align one-to-one with expected_tools")
        return self

    def as_eval_case(self) -> EvalCase:
        return EvalCase.model_validate(
            {
                "name": self.id,
                "input": self.input,
                "expect": {
                    "contains": [self.expected_output] if self.expected_output else [],
                    "exact_output": self.expected_output,
                    "matches": self.expected_regex,
                    "json_schema": self.expected_json_schema,
                    "tools_called": self.expected_tools,
                    "tools_not_called": self.forbidden_tools,
                    "tool_arguments": self.expected_tool_arguments,
                    "tool_order": self.tool_order,
                    "max_total_tokens": self.max_total_tokens,
                    "max_latency_ms": self.max_latency_ms,
                    "max_cost_usd": self.max_cost_usd,
                },
                "human_scores": self.human_scores,
            }
        )


class DatasetCreate(EvaluationModel):
    name: Name
    version: Version
    description: Annotated[str, Field(max_length=10_000)] = ""
    cases: Annotated[list[DatasetCase], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def unique_case_ids(self) -> DatasetCreate:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset case ids must be unique")
        serialized = json.dumps(
            [case.model_dump(mode="json") for case in self.cases],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(serialized) > _MAX_DATASET_CASE_BYTES:
            raise ValueError("dataset case payload must not exceed 1 MiB")
        return self


class DatasetVersion(EvaluationModel):
    name: Name
    version: Version
    description: str = ""
    cases: list[DatasetCase] = Field(default_factory=list)
    case_count: int = Field(ge=0)
    content_hash: str
    blob_key: str
    owner_scope: str
    created_at: str


class EvalThresholds(EvaluationModel):
    success: float | None = Field(default=None, ge=0, le=1)
    safety: float | None = Field(default=None, ge=0, le=1)
    p95_latency_s: float | None = Field(default=None, gt=0)
    cost_per_run_usd: float | None = Field(default=None, ge=0)


class JudgeRubric(EvaluationModel):
    name: Name
    version: Version
    dimensions: Annotated[dict[JudgeDimension, str], Field(min_length=1, max_length=5)]


class JudgeConfig(EvaluationModel):
    provider: Name
    model: Annotated[str, Field(min_length=1, max_length=200)]
    rubric: JudgeRubric
    pass_threshold: float = Field(default=0.7, ge=0, le=1)
    max_tokens: int = Field(default=800, ge=64, le=4096)
    max_cost_per_case_usd: float = Field(default=0.05, gt=0, le=10)
    timeout_seconds: float = Field(default=30, gt=0, le=60)


class EvalSuiteCreate(EvaluationModel):
    name: Name
    version: Version
    description: Annotated[str, Field(max_length=10_000)] = ""
    dataset: ArtifactVersionRef
    scorers: Annotated[list[Name], Field(min_length=1, max_length=50)]
    thresholds: EvalThresholds = Field(default_factory=EvalThresholds)
    judge: JudgeConfig | None = None

    @model_validator(mode="after")
    def unique_scorers(self) -> EvalSuiteCreate:
        if len(self.scorers) != len(set(self.scorers)):
            raise ValueError("eval suite scorers must be unique")
        if ("llm_judge" in self.scorers) != (self.judge is not None):
            raise ValueError("llm_judge scorer requires exactly one pinned judge configuration")
        return self


class EvalSuite(EvaluationModel):
    name: Name
    version: Version
    description: str = ""
    dataset: ArtifactVersionRef
    scorers: list[str]
    thresholds: EvalThresholds
    judge: JudgeConfig | None = None
    owner_scope: str
    created_at: str


class ResolvedEvaluation(EvaluationModel):
    cases: list[EvalCase]
    dataset: ArtifactVersionRef
    suite: ArtifactVersionRef | None = None
    scorers: list[str] = Field(default_factory=list)
    thresholds: EvalThresholds = Field(default_factory=EvalThresholds)
    judge: JudgeConfig | None = None


class EvaluationRunCreate(EvaluationModel):
    suite: ArtifactVersionRef
    sandbox_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    sandbox: SandboxSpec | None = None

    @model_validator(mode="after")
    def exactly_one_sandbox_source(self) -> EvaluationRunCreate:
        if (self.sandbox_id is None) == (self.sandbox is None):
            raise ValueError("provide exactly one of sandbox_id or sandbox")
        return self


ComparisonAxisName = Literal["prompt_version", "model", "agent_version", "tool_config"]


def _default_comparison_axes() -> list[ComparisonAxisName]:
    return ["prompt_version", "model", "agent_version", "tool_config"]


class ComparisonCreate(EvaluationModel):
    baseline_run_id: Annotated[str, Field(min_length=1, max_length=100)]
    candidate_run_id: Annotated[str, Field(min_length=1, max_length=100)]
    axes: list[ComparisonAxisName] = Field(
        default_factory=_default_comparison_axes,
        min_length=1,
        max_length=4,
    )

    @model_validator(mode="after")
    def distinct_runs_and_axes(self) -> ComparisonCreate:
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("baseline and candidate eval runs must differ")
        if len(self.axes) != len(set(self.axes)):
            raise ValueError("comparison axes must be unique")
        return self


class ComparisonMetric(EvaluationModel):
    baseline: float
    candidate: float
    delta: float
    percent_delta: float | None = None


class ComparisonAxis(EvaluationModel):
    baseline: Any = None
    candidate: Any = None
    changed: bool


class ComparisonCase(EvaluationModel):
    case_id: str
    baseline_passed: bool
    candidate_passed: bool
    baseline_trace_url: str | None = None
    candidate_trace_url: str | None = None


class EvaluationComparison(EvaluationModel):
    id: str
    baseline_run_id: str
    candidate_run_id: str
    dataset: dict[str, str]
    metrics: dict[str, ComparisonMetric]
    axes: dict[ComparisonAxisName, ComparisonAxis]
    changed_cases: list[ComparisonCase]
    regressions: list[ComparisonCase]
    newly_passing: list[ComparisonCase]
    sample_size: int = Field(ge=0)
    caveat: str
    summary: str
    created_at: str


__all__ = [
    "ArtifactVersionRef",
    "DatasetCase",
    "DatasetCreate",
    "DatasetVersion",
    "EvalSuite",
    "EvalSuiteCreate",
    "EvalThresholds",
    "ComparisonAxis",
    "ComparisonAxisName",
    "ComparisonCase",
    "ComparisonCreate",
    "ComparisonMetric",
    "EvaluationComparison",
    "EvaluationRunCreate",
    "JudgeConfig",
    "JudgeDimension",
    "JudgeRubric",
    "ResolvedEvaluation",
]
