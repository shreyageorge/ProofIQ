"""Deterministic structured-intent answerability tests."""

from core.answerability import assess_answerability
from models.schemas import (
    AnswerabilityStatus,
    QuestionIntent,
    RequestedCapability,
)


def test_available_required_fields_are_answerable(
    analysis_dataset, analysis_column_ids
) -> None:
    result = assess_answerability(
        analysis_dataset,
        QuestionIntent(
            question_id="q1",
            dataset_id=analysis_dataset.manifest.dataset_id,
            required_fields=("amount", "region"),
            capabilities=(RequestedCapability.GROUP, RequestedCapability.AGGREGATE),
        ),
    )

    assert result.status is AnswerabilityStatus.ANSWERABLE
    assert result.resolved_table_id == analysis_dataset.tables[0].table_id
    assert set(result.resolved_column_ids) == {
        analysis_column_ids["amount"],
        analysis_column_ids["region"],
    }


def test_missing_column_cannot_be_determined(analysis_dataset) -> None:
    result = assess_answerability(
        analysis_dataset,
        QuestionIntent(
            question_id="q2",
            dataset_id=analysis_dataset.manifest.dataset_id,
            required_fields=("profit",),
        ),
    )

    assert result.status is AnswerabilityStatus.CANNOT_DETERMINE
    assert result.missing_fields == ("profit",)
    assert any("substituted" in limitation for limitation in result.limitations)


def test_case_insensitive_ambiguous_column_requires_clarification() -> None:
    from core.ingestion import ingest_upload

    dataset = ingest_upload(
        "ambiguous.csv",
        b"Region,region,amount\nNorth,North,2\n",
    )
    result = assess_answerability(
        dataset,
        QuestionIntent(
            question_id="q3",
            dataset_id=dataset.manifest.dataset_id,
            required_fields=("region",),
        ),
    )

    assert result.status is AnswerabilityStatus.NEEDS_CLARIFICATION


def test_unsupported_operation_cannot_be_determined(analysis_dataset) -> None:
    result = assess_answerability(
        analysis_dataset,
        QuestionIntent(
            question_id="q4",
            dataset_id=analysis_dataset.manifest.dataset_id,
            required_fields=("amount",),
            capabilities=(RequestedCapability.JOIN,),
        ),
    )

    assert result.status is AnswerabilityStatus.CANNOT_DETERMINE
    assert "join" in result.reasons[0]


def test_missing_optional_field_is_partially_answerable(analysis_dataset) -> None:
    result = assess_answerability(
        analysis_dataset,
        QuestionIntent(
            question_id="q5",
            dataset_id=analysis_dataset.manifest.dataset_id,
            required_fields=("amount",),
            optional_fields=("profit",),
        ),
    )

    assert result.status is AnswerabilityStatus.PARTIALLY_ANSWERABLE
    assert result.missing_fields == ("profit",)


def test_multiple_matching_tables_require_clarification(multi_sheet_xlsx_bytes) -> None:
    from core.ingestion import ingest_upload

    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    result = assess_answerability(
        dataset,
        QuestionIntent(
            question_id="q6",
            dataset_id=dataset.manifest.dataset_id,
        ),
    )

    assert result.status is AnswerabilityStatus.NEEDS_CLARIFICATION


def test_unstructured_question_intent_requires_clarification(analysis_dataset) -> None:
    result = assess_answerability(
        analysis_dataset,
        QuestionIntent(
            question_id="q7",
            dataset_id=analysis_dataset.manifest.dataset_id,
        ),
    )
    assert result.status is AnswerabilityStatus.NEEDS_CLARIFICATION
