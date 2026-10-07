"""Typed analysis plans and strict validation against ingested snapshots."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time

from core.profiler import profile_dataset
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    AnswerabilityStatus,
    Filter,
    FilterOperator,
    GroupAggregate,
    IngestedDataset,
    Limit,
    PhysicalType,
    PlanValidation,
    Project,
    OutputSpecification,
    SemanticHint,
    SelectTable,
    Sort,
    SortKey,
)

MAX_PLAN_STEPS = 12
MAX_RESULT_ROWS = 10_000
MAX_FILTER_VALUES = 1_000
MAX_SORT_KEYS = 500
_CODE_SHAPED_VALUE = re.compile(
    r"__import__|\b(?:df|pd|os|subprocess)\s*\[|"
    r"\b[A-Za-z_]\w*\s*\([^)]*\)\s*\.\s*[A-Za-z_]\w*\s*\(|"
    r"\b[A-Za-z_]\w*\s*\([^)]*\)",
    re.IGNORECASE,
)
_NUMERIC_TYPES = {PhysicalType.INTEGER, PhysicalType.FLOAT}
_NUMERIC_AGGREGATIONS = {
    AggregationFunction.SUM,
    AggregationFunction.MEAN,
    AggregationFunction.MIN,
    AggregationFunction.MAX,
}


def validate_plan(
    dataset: IngestedDataset,
    plan: AnalysisPlan,
    *,
    max_steps: int = MAX_PLAN_STEPS,
    max_limit: int = MAX_RESULT_ROWS,
) -> PlanValidation:
    """Validate every plan reference and operation before execution."""
    issues: list[str] = []
    if not isinstance(dataset, IngestedDataset):
        return _rejected("A valid ingested dataset is required.")
    if not isinstance(plan, AnalysisPlan):
        return _rejected("The plan has an invalid contract.")
    if not isinstance(max_steps, int) or max_steps < 1:
        return _rejected("max_steps must be a positive integer.")
    if not isinstance(max_limit, int) or max_limit < 1:
        return _rejected("max_limit must be a positive integer.")
    if plan.dataset_id != dataset.manifest.dataset_id:
        issues.append("The plan dataset does not match the supplied dataset.")
    if (
        not isinstance(plan.plan_id, str)
        or not isinstance(plan.question_id, str)
        or not plan.plan_id.strip()
        or not plan.question_id.strip()
    ):
        issues.append("plan_id and question_id must not be empty.")
    if type(plan.version) is not int or plan.version != 1:
        issues.append("Only plan version 1 is supported.")
    if not isinstance(plan.steps, tuple) or not plan.steps:
        issues.append("A plan must contain an ordered tuple of steps.")
    elif len(plan.steps) > max_steps:
        issues.append(f"The plan exceeds the {max_steps}-step limit.")
    if not isinstance(plan.assumptions, tuple) or any(
        not isinstance(value, str) for value in plan.assumptions
    ):
        issues.append("Plan assumptions must be a tuple of strings.")

    if issues:
        return _rejected(*issues)

    refs = {ref.table_id: ref for ref in dataset.manifest.tables}
    tables = {table.table_id: table for table in dataset.tables}
    profiles = profile_dataset(dataset)
    table_profiles = {table.table_id: table for table in profiles.tables}
    if len(plan.steps) == 0 or type(plan.steps[0]) is not SelectTable:
        return _rejected("The first plan step must select exactly one table.")
    select = plan.steps[0]
    if not isinstance(select.table_id, str) or (
        select.table_id not in refs or select.table_id not in tables
    ):
        return _rejected(f"Unknown table ID: {select.table_id!r}.")

    snapshot = tables[select.table_id]
    table_profile = table_profiles[select.table_id]
    columns = {column.column_id: column for column in table_profile.columns}
    names = {column.column_id: column.name for column in table_profile.columns}
    if len(plan.steps) > max_steps:
        return _rejected(f"The plan exceeds the {max_steps}-step limit.")
    if sum(type(step) is SelectTable for step in plan.steps) != 1:
        issues.append("A plan must select exactly one table.")

    phase = 0
    group_step: GroupAggregate | None = None
    sort_seen = limit_seen = project_seen = False
    current_ids = set(columns)
    expected_columns = tuple(snapshot.columns)

    for index, step in enumerate(plan.steps[1:], start=1):
        step_type = type(step)
        if step_type is Filter:
            if phase > 1:
                issues.append("Filters must precede aggregation, sorting, limit, and projection.")
                continue
            phase = 1
            _validate_filter(step, columns, issues)
        elif step_type is GroupAggregate:
            if phase > 2 or group_step is not None:
                issues.append("At most one aggregation is supported, before sorting.")
                continue
            phase = 2
            group_step = step
            if not isinstance(step.group_by, tuple) or any(
                not isinstance(column_id, str) for column_id in step.group_by
            ):
                issues.append("group_by must be a tuple of column IDs.")
                continue
            if len(set(step.group_by)) != len(step.group_by):
                issues.append("group_by must be a tuple of unique column IDs.")
            for column_id in step.group_by:
                if column_id not in columns:
                    issues.append(f"Unknown group-by column ID: {column_id!r}.")
            _validate_aggregation(step.aggregation, columns, issues)
            group_names = tuple(
                names[column_id]
                for column_id in step.group_by
                if column_id in names
            )
            if isinstance(step.aggregation, Aggregation):
                if step.aggregation.output_name in group_names:
                    issues.append("Aggregation output name collides with a group column.")
                expected_columns = group_names + (step.aggregation.output_name,)
            else:
                expected_columns = group_names
            current_ids = set(step.group_by)
        elif step_type is Sort:
            if phase > 3 or sort_seen:
                issues.append("At most one sort step is supported before limit.")
                continue
            phase = 3
            sort_seen = True
            _validate_sort(step, columns, group_step, issues)
        elif step_type is Limit:
            if phase > 4 or limit_seen:
                issues.append("At most one limit step is supported.")
                continue
            phase = 4
            limit_seen = True
            if type(step.count) is not int or not 1 <= step.count <= max_limit:
                issues.append(f"Limit must be an integer from 1 to {max_limit}.")
        elif step_type is Project:
            if phase > 5 or project_seen or index != len(plan.steps) - 1:
                issues.append("Projection may appear once and must be the final step.")
                continue
            phase = 5
            project_seen = True
            if (
                not isinstance(step.column_ids, tuple)
                or not step.column_ids
                or any(not isinstance(column_id, str) for column_id in step.column_ids)
                or len(set(step.column_ids)) != len(step.column_ids)
            ):
                issues.append("Projection requires a non-empty tuple of unique column IDs.")
                continue
            invalid = tuple(column_id for column_id in step.column_ids if column_id not in current_ids)
            if invalid:
                issues.append(f"Unknown or unavailable projected column ID: {invalid[0]!r}.")
                continue
            expected_columns = tuple(names[column_id] for column_id in step.column_ids)
            if group_step is not None and isinstance(group_step.aggregation, Aggregation):
                expected_columns += (group_step.aggregation.output_name,)
            current_ids = set(step.column_ids)
        else:
            issues.append(f"Unsupported or malformed step at position {index}.")

    if type(plan.output_spec) is not OutputSpecification:
        issues.append("An output specification is required.")
    elif plan.output_spec.columns is not None:
        if (
            not isinstance(plan.output_spec.columns, tuple)
            or any(not isinstance(name, str) for name in plan.output_spec.columns)
        ):
            issues.append("Output columns must be a tuple of names or None.")
        elif plan.output_spec.columns != expected_columns:
            issues.append(
                "Output specification does not match the columns produced by the plan."
            )

    if issues:
        return PlanValidation(
            accepted=False,
            validated_plan=None,
            issues=tuple(dict.fromkeys(issues)),
            answerability_status=AnswerabilityStatus.CANNOT_DETERMINE,
        )
    return PlanValidation(
        accepted=True,
        validated_plan=plan,
        answerability_status=AnswerabilityStatus.ANSWERABLE,
    )


def _validate_filter(
    step: Filter, columns: Mapping[str, object], issues: list[str]
) -> None:
    if not isinstance(step.column_id, str):
        issues.append("Filter column ID must be a string.")
        return
    if step.column_id not in columns:
        issues.append(f"Unknown filter column ID: {step.column_id!r}.")
        return
    if type(step.operator) is not FilterOperator:
        issues.append("Filter operator is not supported.")
        return
    column = columns[step.column_id]
    physical_type = column.physical_type
    semantic_hints = column.semantic_hints
    if step.operator is FilterOperator.IS_NULL:
        if step.value is not None:
            issues.append("IS_NULL requires value=None.")
        return
    if step.operator is FilterOperator.IN:
        if not isinstance(step.value, tuple) or not step.value:
            issues.append("IN requires a non-empty tuple of literal values.")
            return
        if len(step.value) > MAX_FILTER_VALUES:
            issues.append(f"IN supports at most {MAX_FILTER_VALUES} literal values.")
            return
        values = step.value
    else:
        if isinstance(step.value, tuple) or step.value is None:
            issues.append(f"{step.operator.value} requires one non-null literal value.")
            return
        values = (step.value,)
    if any(not _valid_literal(value) for value in values):
        issues.append("Filter values must be finite scalar literals.")
        return
    if any(isinstance(value, str) and _CODE_SHAPED_VALUE.search(value) for value in values):
        issues.append("Expression-like strings are not valid filter literals.")
        return
    numeric_column = _has_numeric_values(column)
    if step.operator in {FilterOperator.GT, FilterOperator.GTE, FilterOperator.LT, FilterOperator.LTE}:
        if not numeric_column or any(not _is_number(value) for value in values):
            issues.append("Ordered comparisons require a numeric column and numeric literal.")
        return
    for value in values:
        if numeric_column:
            if _is_number(value):
                continue
            issues.append(
                f"Filter value type is incompatible with numeric column {column.name!r}."
            )
            break
        if type(value) is str and physical_type is PhysicalType.STRING:
            continue
        if type(value) is bool and physical_type is PhysicalType.BOOLEAN:
            continue
        observed_type = {
            PhysicalType.DATE: date,
            PhysicalType.DATETIME: datetime,
            PhysicalType.TIME: time,
        }.get(physical_type)
        if observed_type is not None and type(value) is observed_type:
            continue
        if physical_type is PhysicalType.MIXED:
            continue
        issues.append(
            f"Filter value type is incompatible with column {column.name!r}."
        )
        break


def _validate_aggregation(
    aggregation: object, columns: Mapping[str, object], issues: list[str]
) -> None:
    if type(aggregation) is not Aggregation:
        issues.append("Aggregation specification is malformed.")
        return
    if type(aggregation.function) is not AggregationFunction:
        issues.append("Aggregation function is not supported.")
        return
    if (
        not isinstance(aggregation.output_name, str)
        or not aggregation.output_name.strip()
        or _CODE_SHAPED_VALUE.search(aggregation.output_name)
    ):
        issues.append("Aggregation output name must be a plain non-empty label.")
    if aggregation.function is AggregationFunction.COUNT and aggregation.column_id is None:
        return
    if aggregation.column_id is not None and not isinstance(
        aggregation.column_id, str
    ):
        issues.append("Aggregation column ID must be a string.")
        return
    if aggregation.column_id not in columns:
        issues.append(f"Unknown aggregation column ID: {aggregation.column_id!r}.")
        return
    column = columns[aggregation.column_id]
    if aggregation.function in _NUMERIC_AGGREGATIONS and not _is_numeric_measure(column):
        issues.append(
            f"{aggregation.function.value} requires a numeric column; "
            f"{column.name!r} is not known to be numeric."
        )


def _validate_sort(
    step: Sort,
    columns: Mapping[str, object],
    group_step: GroupAggregate | None,
    issues: list[str],
) -> None:
    if not isinstance(step.keys, tuple) or not step.keys:
        issues.append("Sort requires at least one typed sort key.")
        return
    if len(step.keys) > MAX_SORT_KEYS:
        issues.append(f"Sort supports at most {MAX_SORT_KEYS} keys.")
        return
    seen: set[tuple[str, str]] = set()
    for key in step.keys:
        if type(key) is not SortKey or type(key.ascending) is not bool:
            issues.append("Sort keys must use the SortKey contract.")
            continue
        if (key.column_id is None) == (key.output_name is None):
            issues.append("Each sort key must reference exactly one column or output.")
            continue
        if key.column_id is not None:
            if not isinstance(key.column_id, str):
                issues.append("Sort column ID must be a string.")
            else:
                if key.column_id not in columns:
                    issues.append(f"Unknown sort column ID: {key.column_id!r}.")
                elif group_step is not None and key.column_id not in group_step.group_by:
                    issues.append("After aggregation, only group-by columns may be sorted by ID.")
                elif key.column_id in columns and columns[key.column_id].physical_type in {
                    PhysicalType.EMPTY,
                    PhysicalType.MIXED,
                }:
                    issues.append("Columns with empty or mixed types cannot be sorted.")
            identity = ("column", str(key.column_id))
        else:
            aggregation = group_step.aggregation if group_step is not None else None
            if (
                type(key.output_name) is not str
                or type(aggregation) is not Aggregation
                or key.output_name != aggregation.output_name
            ):
                issues.append(f"Unknown result column for sorting: {key.output_name!r}.")
            identity = ("output", str(key.output_name))
        if identity in seen:
            issues.append("Sort keys must not contain duplicate columns.")
        seen.add(identity)


def _valid_literal(value: object) -> bool:
    if value is None:
        return False
    if type(value) in {str, bool, int, date, datetime, time}:
        return True
    return type(value) is float and math.isfinite(value)


def _is_number(value: object) -> bool:
    return type(value) in {int, float} and (
        type(value) is int or math.isfinite(value)
    )


def _has_numeric_values(column: object) -> bool:
    return column.physical_type in _NUMERIC_TYPES or (
        column.physical_type is PhysicalType.STRING
        and SemanticHint.NUMERIC in column.semantic_hints
    )


def _is_numeric_measure(column: object) -> bool:
    return _has_numeric_values(column) and not {
        SemanticHint.IDENTIFIER_LIKE,
        SemanticHint.AMBIGUOUS,
    }.intersection(column.semantic_hints)


def _rejected(*issues: str) -> PlanValidation:
    return PlanValidation(
        accepted=False,
        validated_plan=None,
        issues=tuple(issues),
        answerability_status=AnswerabilityStatus.CANNOT_DETERMINE,
    )
