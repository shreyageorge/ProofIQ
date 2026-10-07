"""Tests for deterministic dataset profiling."""

import pytest

from core.ingestion import ingest_upload
from core.profiler import profile_dataset
from core.schema import infer_physical_type
from models.schemas import PhysicalType, QuestionRequest, SemanticHint


def test_profiler_counts_nulls_duplicates_and_semantics(
    missing_duplicates_csv_bytes: bytes,
) -> None:
    dataset = ingest_upload("quality.csv", missing_duplicates_csv_bytes)
    profile = profile_dataset(dataset)
    table = profile.tables[0]
    columns = {column.name: column for column in table.columns}

    assert table.row_count == 3
    assert table.column_count == 3
    assert table.duplicate_row_count == 1
    assert columns["item"].null_count == 0
    assert columns["qty"].null_count == 1
    assert columns["qty"].distinct_count == 1
    assert columns["qty"].physical_type == PhysicalType.STRING
    assert SemanticHint.NUMERIC in columns["qty"].semantic_hints
    assert columns["note"].sample_values == ("x",)
    assert columns["item"].source_lineage.source_sheet is None


def test_profiler_reports_mixed_types_and_avoids_ambiguous_date_claims(
    mixed_ambiguous_csv_bytes: bytes,
) -> None:
    dataset = ingest_upload("mixed.csv", mixed_ambiguous_csv_bytes)
    profile = profile_dataset(dataset)
    columns = {column.name: column for column in profile.tables[0].columns}

    assert columns["value"].physical_type == PhysicalType.STRING
    assert SemanticHint.NUMERIC not in columns["value"].semantic_hints
    assert SemanticHint.AMBIGUOUS in columns["value"].semantic_hints
    assert SemanticHint.DATE_TIME not in columns["date_text"].semantic_hints
    assert SemanticHint.CATEGORICAL in columns["date_text"].semantic_hints


def test_profiler_samples_are_bounded_and_deterministic(
    valid_csv_bytes: bytes,
) -> None:
    dataset = ingest_upload("sales.csv", valid_csv_bytes)
    first = profile_dataset(dataset, sample_limit=2)
    second = profile_dataset(dataset, sample_limit=2)
    first_columns = first.tables[0].columns
    second_columns = second.tables[0].columns

    assert [column.sample_values for column in first_columns] == [
        column.sample_values for column in second_columns
    ]
    assert all(len(column.sample_values) <= 2 for column in first_columns)
    assert first_columns[0].sample_values == ("North", "South")


def test_xlsx_profile_recognizes_physical_types_and_lineage(
    multi_sheet_xlsx_bytes: bytes,
) -> None:
    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    profile = profile_dataset(dataset)

    sales_formula = profile.tables[0].columns[2]
    mixed = profile.tables[0].columns[3]
    enabled = profile.tables[1].columns[0]
    created = profile.tables[1].columns[1]
    assert sales_formula.sample_values == ("=1+1",)
    assert mixed.physical_type == PhysicalType.MIXED
    assert enabled.physical_type == PhysicalType.BOOLEAN
    assert SemanticHint.BOOLEAN_LIKE in enabled.semantic_hints
    assert created.physical_type == PhysicalType.DATETIME
    assert SemanticHint.DATE_TIME in created.semantic_hints
    assert created.source_lineage.source_sheet == "Inventory"


def test_profiler_rejects_unbounded_sample_requests(valid_csv_bytes: bytes) -> None:
    dataset = ingest_upload("sales.csv", valid_csv_bytes)
    with pytest.raises(ValueError, match="sample_limit"):
        profile_dataset(dataset, sample_limit=11)


def test_identifier_hint_uses_explicit_name_tokens_only() -> None:
    dataset = ingest_upload(
        "ids.csv",
        b"customer_id,paid\n001,2\n002,3\n",
    )
    columns = {
        column.name: column
        for column in profile_dataset(dataset).tables[0].columns
    }

    assert SemanticHint.IDENTIFIER_LIKE in columns["customer_id"].semantic_hints
    assert SemanticHint.IDENTIFIER_LIKE not in columns["paid"].semantic_hints


def test_mixed_date_and_datetime_values_are_not_promoted_to_one_type() -> None:
    from datetime import date, datetime

    assert infer_physical_type(
        (date(2025, 1, 2), datetime(2025, 1, 2, 12, 0))
    ) is PhysicalType.MIXED


def test_question_request_is_bound_to_a_dataset() -> None:
    request = QuestionRequest(
        question_id="question-1",
        text="What is the total?",
        dataset_id="dataset-1",
    )

    assert request.dataset_id == "dataset-1"
    assert request.locale is None

    with pytest.raises(ValueError, match="question text"):
        QuestionRequest("question-2", "  ", "dataset-1")
