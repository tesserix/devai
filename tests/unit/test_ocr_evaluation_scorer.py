from __future__ import annotations

from devai.evaluations.scorers import ScorerContext, bind
from devai.sandbox.evals import EvalExpect, OCRExpect, OCRTableCell
from devai.sandbox.trace import Invocation, TraceStep


def _invocation(output: dict[str, object]) -> Invocation:
    return Invocation(
        id="inv-ocr",
        sandbox_id="sb-ocr",
        agent="document-agent",
        final_text="The document was extracted with citations.",
        steps=[TraceStep(kind="tool", name="extract_document", output=output)],
    )


def test_ocr_quality_scores_text_fields_tables_classification_and_citations() -> None:
    output: dict[str, object] = {
        "job_id": "job_EVAL",
        "status": "completed",
        "content_trust": "untrusted",
        "text": "Invoice total 1280.50 AUD",
        "classification": {"type": "invoice", "confidence": 0.98},
        "fields": [
            {
                "name": "total",
                "value_json": '{"currency":"AUD","decimal":"1280.50"}',
                "confidence": 0.96,
                "citations": [
                    {
                        "document_version": f"sha256:{'a' * 64}",
                        "page": 1,
                        "polygon": [[0.7, 0.8], [0.9, 0.8], [0.9, 0.85]],
                        "observation_id": "obs_TOTAL",
                    }
                ],
            }
        ],
        "tables": [
            {
                "table_id": "table_1",
                "cells": [{"row": 0, "column": 0, "text": "Item"}],
            }
        ],
    }
    expectation = OCRExpect(
        reference_text="Invoice total 1280.50 AUD",
        expected_fields={"total": {"currency": "AUD", "decimal": "1280.50"}},
        expected_table_cells=[OCRTableCell(row=0, column=0, text="Item")],
        expected_document_type="invoice",
        max_character_error_rate=0.01,
        max_word_error_rate=0.01,
        min_field_f1=0.99,
        min_table_cell_accuracy=0.99,
    )

    result = bind(["ocr_quality"])[0].score(
        ScorerContext(invocation=_invocation(output), expect=EvalExpect(ocr=expectation))
    )

    assert result.passed
    assert result.score == 1.0
    assert result.detail == {
        "character_error_rate": 0.0,
        "word_error_rate": 0.0,
        "field_precision": 1.0,
        "field_recall": 1.0,
        "field_f1": 1.0,
        "table_cell_accuracy": 1.0,
        "classification_accuracy": 1.0,
        "citation_coverage": 1.0,
    }


def test_ocr_quality_fails_closed_without_exposing_document_values_in_details() -> None:
    output: dict[str, object] = {
        "job_id": "job_EVAL",
        "status": "completed",
        "content_trust": "trusted",
        "text": "private wrong value",
        "fields": [],
    }
    expectation = OCRExpect(
        reference_text="private expected value",
        expected_fields={"account_number": "123456"},
        max_character_error_rate=0.0,
        max_word_error_rate=0.0,
        min_field_f1=1.0,
    )

    result = bind(["ocr_quality"])[0].score(
        ScorerContext(invocation=_invocation(output), expect=EvalExpect(ocr=expectation))
    )

    assert not result.passed
    assert result.score < 1.0
    assert "private" not in str(result.detail)
    assert "123456" not in str(result.detail)
    assert result.detail["contract_errors"] == ["content_trust"]
