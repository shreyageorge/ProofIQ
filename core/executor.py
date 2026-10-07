"""Trusted interpreter for validated typed plans using bounded Pandas operations."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

import pandas as pd

from core.planner import MAX_RESULT_ROWS, validate_plan
from core.profiler import profile_dataset
from core.schema import stable_id
from models.schemas import (
    AggregationFunction,
    ExecutionStatus,
    ExecutionResult,
    Filter,
    FilterOperator,
    GroupAggregate,
    IngestedDataset,
    Limit,
    PlanValidation,
    PhysicalType,
    Project,
    ResultTable,
    SelectTable,
    Sort,
)


class ExecutionError(ValueError):
    """Raised when a validated plan cannot safely produce a bounded result."""


def execute_plan(
    dataset: IngestedDataset,
    validation: PlanValidation,
    *,
    max_result_rows: int = MAX_RESULT_ROWS,
) -> ExecutionResult:
    """Execute only a plan that passes validation against this dataset."""
    if type(max_result_rows) is not int or max_result_rows < 1:
        raise ExecutionError("max_result_rows must be a positive integer.")
    if type(validation) is not PlanValidation or not validation.accepted:
        raise ExecutionError("Execution requires an accepted plan validation.")
    plan = validation.validated_plan
    if plan is None:
        raise ExecutionError("Accepted validation has no validated plan.")
    revalidated = validate_plan(dataset, plan, max_limit=max_result_rows)
    if not revalidated.accepted or revalidated.validated_plan != plan:
        raise ExecutionError("The plan is no longer valid for this dataset.")

    started = time.perf_counter()
    snapshot = next(
        table for table in dataset.tables if table.table_id == plan.steps[0].table_id
    )
    profile = next(
        table
        for table in profile_dataset(dataset).tables
        if table.table_id == snapshot.table_id
    )
    columns = {column.column_id: column.name for column in profile.columns}
    semantic_numeric = {
        column.column_id
        for column in profile.columns
        if any(hint.value == "numeric" for hint in column.semantic_hints)
    }
    mixed_columns = {
        column.column_id
        for column in profile.columns
        if column.physical_type is PhysicalType.MIXED
    }
    frame = pd.DataFrame(snapshot.rows, columns=snapshot.columns)
    frame.index = pd.Index(snapshot.row_ids, name="source_row_id")
    lineage: dict[object, tuple[str, ...]] = {
        row_id: (row_id,) for row_id in snapshot.row_ids
    }
    warnings: list[str] = []
    group_step: GroupAggregate | None = None

    for step in plan.steps[1:]:
        if type(step) is Filter:
            frame = _filter_frame(
                frame, step, columns, semantic_numeric, mixed_columns
            )
        elif type(step) is GroupAggregate:
            group_step = step
            frame, lineage = _group_frame(frame, lineage, step, columns)
            warnings.append(
                "Null values are excluded from column counts and aggregations; "
                "SUM over only-null values is null."
            )
        elif type(step) is Sort:
            sort_frame = frame.copy()
            sort_columns: list[str] = []
            for key_index, key in enumerate(step.keys):
                source_name = (
                    columns[key.column_id]
                    if key.column_id is not None
                    else key.output_name
                )
                sort_name = f"__proofiq_sort_{key_index}"
                while sort_name in sort_frame.columns:
                    sort_name += "_"
                if (
                    key.column_id is not None
                    and key.column_id in semantic_numeric
                ):
                    sort_frame[sort_name] = _numeric_series(sort_frame[source_name])
                else:
                    sort_frame[sort_name] = sort_frame[source_name]
                sort_columns.append(sort_name)
            ascending = [key.ascending for key in step.keys]
            sorted_index = sort_frame.sort_values(
                by=sort_columns,
                ascending=ascending,
                kind="mergesort",
                na_position="last",
            ).index
            frame = frame.loc[sorted_index]
            warnings.append("Null sort values are ordered last in either direction.")
        elif type(step) is Limit:
            frame = frame.head(step.count)
        elif type(step) is Project:
            selected_names = [columns[column_id] for column_id in step.column_ids]
            if group_step is not None:
                selected_names.append(group_step.aggregation.output_name)
            frame = frame.loc[:, selected_names]
        elif type(step) is SelectTable:
            raise ExecutionError("A plan may select a table only in its first step.")
        else:
            raise ExecutionError("Encountered an unsupported plan step.")

    if len(frame.index) > max_result_rows:
        raise ExecutionError(
            f"Result exceeds the {max_result_rows}-row execution limit."
        )

    row_ids = tuple(tuple(lineage[index]) for index in frame.index)
    result_rows = tuple(
        tuple(_to_cell(value) for value in row)
        for row in frame.itertuples(index=False, name=None)
    )
    result = ResultTable(
        columns=tuple(str(name) for name in frame.columns),
        rows=result_rows,
        row_lineage=row_ids,
    )
    source_ids = _referenced_columns(plan, profile.columns)
    duration_ms = (time.perf_counter() - started) * 1000
    run_id = stable_id(
        "run", f"{dataset.manifest.content_sha256}\0{repr(plan)}"
    )
    return ExecutionResult(
        run_id=run_id,
        plan_id=plan.plan_id,
        status=ExecutionStatus.SUCCESS,
        result=result,
        row_count=len(result.rows),
        warnings=tuple(dict.fromkeys(warnings)),
        duration_ms=duration_ms,
        source_column_ids=source_ids,
    )


def _filter_frame(
    frame: pd.DataFrame,
    step: Filter,
    column_names: dict[str, str],
    numeric_column_ids: set[str],
    mixed_column_ids: set[str],
) -> pd.DataFrame:
    name = column_names[step.column_id]
    series = frame[name]
    if step.operator is FilterOperator.IS_NULL:
        mask = series.isna()
    elif step.operator in {
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
    }:
        numeric = _numeric_series(series)
        operators = {
            FilterOperator.GT: numeric.gt,
            FilterOperator.GTE: numeric.ge,
            FilterOperator.LT: numeric.lt,
            FilterOperator.LTE: numeric.le,
        }
        mask = operators[step.operator](step.value)
    elif step.column_id in numeric_column_ids and (
        isinstance(step.value, tuple) or type(step.value) in {int, float}
    ):
        numeric = _numeric_series(series)
        if step.operator is FilterOperator.IN:
            mask = numeric.isin(step.value)
        else:
            mask = numeric.eq(step.value)
    elif step.column_id in mixed_column_ids:
        values = step.value if isinstance(step.value, tuple) else (step.value,)
        mask = series.map(
            lambda observed: not pd.isna(observed)
            and any(
                type(observed) is type(candidate) and observed == candidate
                for candidate in values
            )
        )
    elif step.operator is FilterOperator.IN:
        mask = series.isin(step.value)
    else:
        mask = series.eq(step.value)
    return frame.loc[mask.fillna(False)]


def _group_frame(
    frame: pd.DataFrame,
    lineage: dict[object, tuple[str, ...]],
    step: GroupAggregate,
    column_names: dict[str, str],
) -> tuple[pd.DataFrame, dict[object, tuple[str, ...]]]:
    aggregation = step.aggregation
    group_names = [column_names[column_id] for column_id in step.group_by]
    target_name = (
        column_names[aggregation.column_id]
        if aggregation.column_id is not None
        else None
    )
    output_columns = group_names + [aggregation.output_name]
    output_rows: list[tuple[object, ...]] = []
    output_lineage: dict[object, tuple[str, ...]] = {}

    if group_names:
        grouped = frame.groupby(
            group_names,
            dropna=False,
            sort=False,
            observed=True,
        )
        for key, group in grouped:
            keys = key if isinstance(key, tuple) else (key,)
            output_index = len(output_rows)
            output_rows.append(
                tuple(_to_cell(value) for value in keys)
                + (_aggregate(group, target_name, aggregation.function),)
            )
            output_lineage[output_index] = tuple(
                row_id
                for source_index in group.index
                for row_id in lineage[source_index]
            )
    else:
        output_rows.append(
            (_aggregate(frame, target_name, aggregation.function),)
        )
        output_lineage[0] = tuple(
            row_id for source_index in frame.index for row_id in lineage[source_index]
        )

    result = pd.DataFrame(output_rows, columns=output_columns)
    result.index = pd.RangeIndex(len(result.index))
    return result, output_lineage


def _aggregate(
    frame: pd.DataFrame,
    target_name: str | None,
    function: AggregationFunction,
) -> object:
    if function is AggregationFunction.COUNT and target_name is None:
        return len(frame.index)
    series = frame[target_name] if target_name is not None else pd.Series(dtype=object)
    if function is AggregationFunction.COUNT:
        return int(series.count())
    if function is AggregationFunction.COUNT_DISTINCT:
        return int(series.nunique(dropna=True))
    numeric = _numeric_series(series)
    if function is AggregationFunction.SUM:
        return numeric.sum(min_count=1)
    if function is AggregationFunction.MEAN:
        return numeric.mean()
    if function is AggregationFunction.MIN:
        return numeric.min()
    if function is AggregationFunction.MAX:
        return numeric.max()
    raise ExecutionError("Unsupported aggregation function.")


def _numeric_series(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.isin((float("inf"), float("-inf"))).any():
        raise ExecutionError(
            "A numeric value is outside the finite range supported by Pandas."
        )
    return numeric


def _referenced_columns(plan, profiles: Sequence[object]) -> tuple[str, ...]:
    used: set[str] = set()
    for step in plan.steps:
        if type(step) is Filter:
            used.add(step.column_id)
        elif type(step) is GroupAggregate:
            used.update(step.group_by)
            if step.aggregation.column_id is not None:
                used.add(step.aggregation.column_id)
        elif type(step) is Sort:
            used.update(key.column_id for key in step.keys if key.column_id is not None)
        elif type(step) is Project:
            used.update(step.column_ids)
    return tuple(column.column_id for column in profiles if column.column_id in used)


def _to_cell(value: object):
    if value is None or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value
