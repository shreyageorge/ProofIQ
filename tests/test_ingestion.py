"""Tests for bounded, immutable upload ingestion."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256

import pytest

from core.ingestion import DEFAULT_INGESTION_LIMITS, ingest_upload
from models.schemas import IngestionError, IngestionErrorCode


def test_csv_ingestion_fingerprints_bytes_and_assigns_stable_row_ids(
    valid_csv_bytes: bytes,
) -> None:
    original = bytes(valid_csv_bytes)
    first = ingest_upload("C:\\uploads\\sales.csv", valid_csv_bytes)
    second = ingest_upload("sales.csv", valid_csv_bytes)

    assert first.manifest.content_sha256 == sha256(original).hexdigest()
    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert first.tables[0].table_id == second.tables[0].table_id
    assert first.tables[0].row_ids == second.tables[0].row_ids
    assert first.tables[0].row_ids[0] != first.tables[0].row_ids[1]
    assert first.manifest.original_name == "sales.csv"
    assert valid_csv_bytes == original


def test_csv_counts_and_in_memory_data_are_immutable(valid_csv_bytes: bytes) -> None:
    dataset = ingest_upload("sales.csv", valid_csv_bytes)
    table = dataset.tables[0]

    assert dataset.manifest.tables[0].row_count == 3
    assert dataset.manifest.tables[0].column_count == 3
    assert table.columns == ("region", "amount", "active")
    assert table.rows[0] == ("North", "100", "true")
    with pytest.raises(TypeError):
        table.rows[0][0] = "changed"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        table.columns = ("changed",)  # type: ignore[misc]


def test_xlsx_ingestion_creates_one_table_per_selected_sheet(
    multi_sheet_xlsx_bytes: bytes,
) -> None:
    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)

    assert [ref.display_name for ref in dataset.manifest.tables] == [
        "Sales",
        "Inventory",
    ]
    assert [table.source_sheet for table in dataset.manifest.tables] == [
        "Sales",
        "Inventory",
    ]
    assert dataset.tables[0].rows[0][2] == "=1+1"
    assert dataset.tables[1].rows[0] == (True, datetime(2025, 1, 2))


def test_xlsx_can_select_one_sheet(multi_sheet_xlsx_bytes: bytes) -> None:
    dataset = ingest_upload(
        "book.xlsx", multi_sheet_xlsx_bytes, selected_sheets=("Inventory",)
    )
    complete = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)

    assert len(dataset.tables) == 1
    assert dataset.tables[0].columns == ("enabled", "created")
    assert dataset.tables[0].table_id == complete.tables[1].table_id


def test_invalid_csv_and_unsupported_format_return_domain_errors(
    malformed_csv_bytes: bytes, unsupported_file_bytes: bytes,
) -> None:
    with pytest.raises(IngestionError) as malformed:
        ingest_upload("broken.csv", malformed_csv_bytes)
    assert malformed.value.code == IngestionErrorCode.INVALID_FILE

    with pytest.raises(IngestionError) as unsupported:
        ingest_upload("data.json", unsupported_file_bytes)
    assert unsupported.value.code == IngestionErrorCode.UNSUPPORTED_FORMAT


def test_duplicate_headers_and_missing_sheet_are_handled_deterministically(
    multi_sheet_xlsx_bytes: bytes,
    duplicate_columns_csv_bytes: bytes,
) -> None:
    with pytest.raises(IngestionError) as bad_sheet:
        ingest_upload("book.xlsx", multi_sheet_xlsx_bytes, selected_sheets=("Unknown",))
    assert bad_sheet.value.code == IngestionErrorCode.SHEET_NOT_FOUND

    duplicate_headers = ingest_upload("dupe.csv", duplicate_columns_csv_bytes)
    assert duplicate_headers.tables[0].columns == (
        "unnamed_column",
        "name",
        "name__2",
        "name__2__2",
    )
    assert duplicate_headers.tables[0].rows == (("first", "second", "third", "fourth"),)


def test_file_row_column_sheet_and_cell_limits_are_enforced(
    valid_csv_bytes: bytes, multi_sheet_xlsx_bytes: bytes
) -> None:
    with pytest.raises(IngestionError) as file_limit:
        ingest_upload(
            "large.csv",
            valid_csv_bytes,
            limits=replace(DEFAULT_INGESTION_LIMITS, max_file_size_bytes=4),
        )
    assert file_limit.value.code == IngestionErrorCode.FILE_TOO_LARGE

    with pytest.raises(IngestionError) as row_limit:
        ingest_upload(
            "rows.csv",
            valid_csv_bytes,
            limits=replace(DEFAULT_INGESTION_LIMITS, max_rows=2),
        )
    assert row_limit.value.code == IngestionErrorCode.LIMIT_EXCEEDED

    with pytest.raises(IngestionError) as column_limit:
        ingest_upload(
            "columns.csv",
            valid_csv_bytes,
            limits=replace(DEFAULT_INGESTION_LIMITS, max_columns=2),
        )
    assert column_limit.value.code == IngestionErrorCode.LIMIT_EXCEEDED

    with pytest.raises(IngestionError) as sheet_limit:
        ingest_upload(
            "book.xlsx",
            multi_sheet_xlsx_bytes,
            limits=replace(DEFAULT_INGESTION_LIMITS, max_xlsx_sheets=1),
        )
    assert sheet_limit.value.code == IngestionErrorCode.LIMIT_EXCEEDED

    with pytest.raises(IngestionError) as cell_limit:
        ingest_upload(
            "cells.csv",
            valid_csv_bytes,
            limits=replace(DEFAULT_INGESTION_LIMITS, max_cells=2),
        )
    assert cell_limit.value.code == IngestionErrorCode.LIMIT_EXCEEDED


def test_corrupt_xlsx_is_a_domain_error() -> None:
    with pytest.raises(IngestionError) as error:
        ingest_upload("corrupt.xlsx", b"not a workbook")
    assert error.value.code == IngestionErrorCode.INVALID_FILE


def test_empty_and_header_only_csv_are_rejected(blank_csv_bytes: bytes) -> None:
    with pytest.raises(IngestionError) as empty:
        ingest_upload("empty.csv", b"")
    assert empty.value.code == IngestionErrorCode.INVALID_FILE

    with pytest.raises(IngestionError) as blank:
        ingest_upload("blank.csv", blank_csv_bytes)
    assert blank.value.code == IngestionErrorCode.EMPTY_DATASET

    with pytest.raises(IngestionError) as header_only:
        ingest_upload("no-rows.csv", b"first,second\n")
    assert header_only.value.code == IngestionErrorCode.EMPTY_DATASET


def test_csv_row_limit_fails_without_truncating(over_row_limit_csv_bytes: bytes) -> None:
    limits = replace(DEFAULT_INGESTION_LIMITS, max_rows=1)
    with pytest.raises(IngestionError) as error:
        ingest_upload("rows.csv", over_row_limit_csv_bytes, limits=limits)
    assert error.value.code == IngestionErrorCode.LIMIT_EXCEEDED
    assert "row limit" in str(error.value)
