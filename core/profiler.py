"""Deterministic profiling for immutable ingested tables."""

from __future__ import annotations

from collections.abc import Sequence

from core.schema import (
    distinct_values,
    infer_physical_type,
    infer_semantic_hints,
    sample_values,
    stable_id,
)
from models.schemas import (
    CellValue,
    ColumnLineage,
    ColumnProfile,
    DatasetProfile,
    IngestedDataset,
    TableProfile,
)

DEFAULT_SAMPLE_LIMIT = 5
MAX_SAMPLE_LIMIT = 10


def profile_dataset(
    dataset: IngestedDataset, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT
) -> DatasetProfile:
    """Build a deterministic catalog without modifying the source snapshot."""
    if not 0 <= sample_limit <= MAX_SAMPLE_LIMIT:
        raise ValueError(f"sample_limit must be between 0 and {MAX_SAMPLE_LIMIT}")
    table_profiles: list[TableProfile] = []
    refs_by_id = {ref.table_id: ref for ref in dataset.manifest.tables}

    for table in dataset.tables:
        ref = refs_by_id[table.table_id]
        column_profiles = tuple(
            _profile_column(
                table.table_id,
                ref.source_file_id,
                ref.source_sheet,
                column_index,
                column_name,
                tuple(row[column_index] for row in table.rows),
                sample_limit,
            )
            for column_index, column_name in enumerate(table.columns)
        )
        table_profiles.append(
            TableProfile(
                table_id=table.table_id,
                row_count=len(table.rows),
                column_count=len(table.columns),
                duplicate_row_count=_duplicate_row_count(table.rows),
                columns=column_profiles,
            )
        )

    return DatasetProfile(
        manifest=dataset.manifest,
        tables=tuple(table_profiles),
    )


def _profile_column(
    table_id: str,
    source_file_id: str,
    source_sheet: str | None,
    column_index: int,
    column_name: str,
    values: Sequence[CellValue],
    sample_limit: int,
) -> ColumnProfile:
    null_count = sum(value is None for value in values)
    physical_type = infer_physical_type(values)
    return ColumnProfile(
        column_id=stable_id("col", f"{table_id}\0{column_index}\0{column_name}"),
        name=column_name,
        physical_type=physical_type,
        semantic_hints=infer_semantic_hints(column_name, values, physical_type),
        null_count=null_count,
        distinct_count=len(distinct_values(values)),
        sample_values=sample_values(values, sample_limit),
        source_lineage=ColumnLineage(
            source_file_id=source_file_id,
            source_sheet=source_sheet,
            source_column_index=column_index,
        ),
    )


def _duplicate_row_count(rows: Sequence[tuple[CellValue, ...]]) -> int:
    seen: set[tuple[tuple[str, object], ...]] = set()
    duplicates = 0
    for row in rows:
        key = tuple((type(value).__name__, value) for value in row)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates
