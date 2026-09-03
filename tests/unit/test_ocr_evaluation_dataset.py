from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from devai.evaluations.models import DatasetArtifact, DatasetCreate, EvalSuiteCreate
from devai.evaluations.scorers import known

DATASET = Path("architecture/evaluation-datasets/ocr-agent-v1.yaml")
SUITE = Path("architecture/evaluation-suites/ocr-agent-v1.yaml")
REQUIRED_COHORTS = {
    "invoice",
    "receipt",
    "handwriting",
    "poor-quality-scan",
    "mixed-language",
    "table",
    "long-document",
    "adversarial",
}
FORBIDDEN_KEYS = {
    "api_key",
    "credential",
    "environment",
    "langfuse_key",
    "product_id",
    "secret",
    "tenant_id",
    "token",
    "url",
}


def _walk(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append((str(key), child))
            found.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk(child))
    return found


def test_ocr_dataset_is_product_neutral_reviewed_and_immutable() -> None:
    body = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    dataset = DatasetCreate.model_validate(body)

    assert dataset.name == "ocr-agent-golden"
    assert dataset.version == "1"
    assert {tag for case in dataset.cases for tag in case.tags} >= REQUIRED_COHORTS
    assert all(case.expected_tools == ["extract_document"] for case in dataset.cases)
    assert all(case.expected_tool_arguments for case in dataset.cases)

    keys = {key.lower() for key, _ in _walk(body)}
    assert keys.isdisjoint(FORBIDDEN_KEYS)

    for case in dataset.cases:
        assert case.artifact is not None
        assert case.artifact.ref.startswith("eval-artifact://ocr-agent/v1/")
        assert case.artifact.provenance == "synthetic"
        assert case.artifact.review_status == "approved"
        assert case.ocr is not None


def test_ocr_release_suite_runs_the_deterministic_quality_and_safety_gates() -> None:
    suite = EvalSuiteCreate.model_validate(yaml.safe_load(SUITE.read_text(encoding="utf-8")))

    assert suite.dataset.name == "ocr-agent-golden"
    assert suite.dataset.version == "1"
    assert "ocr_quality" in suite.scorers
    assert "safety" in suite.scorers
    assert set(suite.scorers) <= set(known())
    assert suite.thresholds.success == 0.92
    assert suite.thresholds.safety == 1.0


@pytest.mark.parametrize(
    ("redacted", "governance_ref"),
    [(False, "approval-123"), (True, None)],
)
def test_reviewed_production_artifact_requires_redaction_and_governance(
    redacted: bool, governance_ref: str | None
) -> None:
    with pytest.raises(ValidationError):
        DatasetArtifact(
            ref="eval-artifact://ocr-agent/v1/reviewed/case-001",
            digest=f"sha256:{'a' * 64}",
            media_type="application/pdf",
            provenance="reviewed-production",
            review_status="approved",
            redacted=redacted,
            governance_ref=governance_ref,
        )
