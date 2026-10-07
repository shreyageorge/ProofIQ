"""Independent standard-library verification of trusted plan results."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key

from core.planner import validate_plan
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
    Project,
    SelectTable,
    Sort,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)

VERIFIER_VERSION = "1.0"
DEFAULT_NUMERIC_TOLERANCE = 1e-9


def verify_result(
    dataset: IngestedDataset,
    validation: PlanValidation,
    result: ExecutionResult | None,
    *,
    tolerance: float = DEFAULT_NUMERIC_TOLERANCE,
) -> VerificationReport:
    """Recompute the supported plan from source tuples without calling executor code."""
    if (
        type(validation) is not PlanValidation
        or not validation.accepted
        or validation.validated_plan is None
        or type(dataset) is not IngestedDataset
        or type(result) is not ExecutionResult
        or type(tolerance) not in {float, int}
        or tolerance < 0
        or tolerance > 1
        or (type(tolerance) is float and not math.isfinite(tolerance))
    ):
        return _not_verified("Verification inputs are unavailable or invalid.")

    plan = validation.validated_plan
    checked = validate_plan(dataset, plan)
    if not checked.accepted or checked.validated_plan != plan:
        return _not_verified("The plan does not validate against this dataset.")
    if result.plan_id != plan.plan_id or result.status is not ExecutionStatus.SUCCESS:
        return _not_verified("The execution result is unavailable for this plan.")

    table_id = plan.steps[0].table_id
    snapshot = next(
        (table for table in dataset.tables if table.table_id == table_id), None
    )
    profile = next(
        (table for table in profile_dataset(dataset).tables if table.table_id == table_id),
        None,
    )
    if snapshot is None or profile is None:
        return _not_verified("The selected source table is unavailable.")
    names = {column.column_id: column.name for column in profile.columns}
    numeric_ids = {
        column.column_id
        for column in profile.columns
        if any(hint.value == "numeric" for hint in column.semantic_hints)
    }
    name_indexes = {name: index for index, name in enumerate(snapshot.columns)}
    rows: list[tuple[object, ...]] = [
        tuple(row) for row in snapshot.rows
    ]
    source_ids = list(snapshot.row_ids)
    filtered_source_ids = list(source_ids)
    for step in plan.steps[1:]:
        if type(step) is Filter:
            column_index = name_indexes[names[step.column_id]]
            rows_and_ids = [
                (row, row_id)
                for row, row_id in zip(rows, source_ids, strict=True)
                if _matches_filter(
                    row[column_index],
                    step,
                    step.column_id in numeric_ids,
                )
            ]
            rows = [row for row, _ in rows_and_ids]
            source_ids = [row_id for _, row_id in rows_and_ids]
            filtered_source_ids = list(source_ids)
        elif type(step) is GroupAggregate:
            rows, source_ids, result_columns = _independent_group(
                rows,
                source_ids,
                step,
                name_indexes,
                names,
                snapshot.columns,
            )
        elif type(step) is Sort:
            has_aggregation = any(
                type(candidate) is GroupAggregate
                for candidate in plan.steps[1 : plan.steps.index(step)]
            )
            group_step = next(
                (
                    candidate
                    for candidate in plan.steps
                    if type(candidate) is GroupAggregate
                ),
                None,
            )
            result_columns = (
                tuple(names[column_id] for column_id in group_step.group_by)
                + (group_step.aggregation.output_name,)
                if has_aggregation and group_step is not None
                else snapshot.columns
            )
            order = _independent_sort_indices(
                rows,
                step,
                names,
                result_columns,
            )
            rows = [rows[index] for index in order]
            source_ids = [source_ids[index] for index in order]
        elif type(step) is Limit:
            rows = rows[: step.count]
            source_ids = source_ids[: step.count]
        elif type(step) is Project:
            group_step = next(
                (candidate for candidate in plan.steps if type(candidate) is GroupAggregate),
                None,
            )
            if group_step is None:
                projection = tuple(name_indexes[names[column_id]] for column_id in step.column_ids)
                rows = [tuple(row[index] for index in projection) for row in rows]
            else:
                group_names = tuple(names[column_id] for column_id in group_step.group_by)
                positions = tuple(group_names.index(names[column_id]) for column_id in step.column_ids)
                rows = [
                    tuple(row[position] for position in positions) + (row[-1],)
                    for row in rows
                ]
        elif type(step) is SelectTable:
            return _not_verified("The plan contains a table selection outside its first step.")

    expected_columns = _result_column_names(plan, snapshot.columns, names)
    expected_rows = tuple(tuple(_to_result_value(value) for value in row) for row in rows)
    has_grouping = any(type(step) is GroupAggregate for step in plan.steps)
    expected_lineage = (
        tuple(tuple(row_ids) for row_ids in source_ids)
        if has_grouping
        else tuple((row_id,) for row_id in source_ids)
    )

    checks: list[VerificationCheck] = []
    schema_ok = (
        result.result.columns == expected_columns
        and all(len(row) == len(expected_columns) for row in result.result.rows)
    )
    checks.append(
        VerificationCheck(
            name="result_schema",
            passed=schema_ok,
            expected=expected_columns,
            observed=result.result.columns,
            detail="Result names and widths match the independently derived schema.",
        )
    )
    count_ok = result.row_count == len(result.result.rows) == len(expected_rows)
    checks.append(
        VerificationCheck(
            name="row_count",
            passed=count_ok,
            expected=len(expected_rows),
            observed=(result.row_count, len(result.result.rows)),
        )
    )
    values_ok = _rows_equal(expected_rows, result.result.rows, float(tolerance))
    checks.append(
        VerificationCheck(
            name="result_values",
            passed=values_ok,
            expected=expected_rows,
            observed=result.result.rows,
            tolerance=float(tolerance),
            detail="Values are independently recomputed from source rows.",
        )
    )
    lineage_missing = (
        len(result.result.row_lineage) == len(expected_lineage)
        and all(not row_lineage for row_lineage in result.result.row_lineage)
        and any(expected_lineage)
    )
    lineage_ok = lineage_missing or result.result.row_lineage == expected_lineage
    checks.append(
        VerificationCheck(
            name="row_lineage",
            passed=lineage_ok,
            expected=expected_lineage,
            observed=result.result.row_lineage,
            detail=(
                "Lineage is absent; numeric result checks remain available."
                if lineage_missing
                else "Source row identifiers match the independently selected rows."
            ),
        )
    )
    filtered_ids = set(filtered_source_ids)
    observed_ids = {
        row_id for group in result.result.row_lineage for row_id in group
    }
    has_filters = any(type(step) is Filter for step in plan.steps)
    filter_ok = not has_filters or observed_ids.issubset(filtered_ids)
    checks.append(
        VerificationCheck(
            name="filter_behavior",
            passed=filter_ok,
            expected=tuple(sorted(filtered_ids)),
            observed=tuple(sorted(observed_ids)),
        )
    )

    group_step = next(
        (step for step in plan.steps if type(step) is GroupAggregate), None
    )
    if group_step is not None:
        group_width = len(group_step.group_by)
        expected_keys = tuple(row[:group_width] for row in expected_rows)
        observed_keys = tuple(row[:group_width] for row in result.result.rows)
        checks.append(
            VerificationCheck(
                name="group_keys",
                passed=_rows_equal(expected_keys, observed_keys, float(tolerance)),
                expected=expected_keys,
                observed=observed_keys,
            )
        )
        expected_aggregates = tuple(row[-1:] for row in expected_rows)
        observed_aggregates = tuple(row[-1:] for row in result.result.rows)
        checks.append(
            VerificationCheck(
                name="aggregate_values",
                passed=_rows_equal(
                    expected_aggregates, observed_aggregates, float(tolerance)
                ),
                expected=expected_aggregates,
                observed=observed_aggregates,
                tolerance=float(tolerance),
            )
        )
    sort_step = next((step for step in plan.steps if type(step) is Sort), None)
    if sort_step is not None:
        checks.append(
            VerificationCheck(
                name="sort_order",
                passed=_rows_equal(expected_rows, result.result.rows, float(tolerance)),
                expected=expected_rows,
                observed=result.result.rows,
            )
        )
    referenced_column_ids = _referenced_column_ids(plan, profile.columns)
    checks.append(
        VerificationCheck(
            name="source_columns",
            passed=result.source_column_ids == referenced_column_ids,
            expected=referenced_column_ids,
            observed=result.source_column_ids,
        )
    )
    limit_step = next((step for step in plan.steps if type(step) is Limit), None)
    if limit_step is not None:
        limit_ok = len(result.result.rows) <= limit_step.count
        checks.append(
            VerificationCheck(
                name="limit",
                passed=limit_ok,
                expected=f"at most {limit_step.count} rows",
                observed=len(result.result.rows),
            )
        )
    expected_run_id = stable_id(
        "run", f"{dataset.manifest.content_sha256}\0{repr(plan)}"
    )
    checks.append(
        VerificationCheck(
            name="run_metadata",
            passed=result.plan_id == plan.plan_id and result.run_id == expected_run_id,
            expected=(plan.plan_id, expected_run_id),
            observed=(result.plan_id, result.run_id),
        )
    )

    failed = tuple(check.name for check in checks if not check.passed)
    if failed:
        return VerificationReport(
            status=VerificationStatus.FAILED,
            checks=tuple(checks),
            reasons=(f"Verification failed for: {', '.join(failed)}.",),
            verifier_version=VERIFIER_VERSION,
        )
    if lineage_missing:
        return VerificationReport(
            status=VerificationStatus.PARTIALLY_VERIFIED,
            checks=tuple(checks),
            reasons=("Result values match, but row lineage was not provided.",),
            verifier_version=VERIFIER_VERSION,
        )
    return VerificationReport(
        status=VerificationStatus.VERIFIED,
        checks=tuple(checks),
        reasons=(
            "The supported computation matches an independent recomputation "
            "against the provided dataset; this does not establish source-data truth "
            "or user-intent correctness.",
        ),
        verifier_version=VERIFIER_VERSION,
    )


def _matches_filter(value: object, step: Filter, numeric: bool) -> bool:
    if step.operator is FilterOperator.IS_NULL:
        return _is_null(value)
    if _is_null(value):
        return False
    candidates = step.value if step.operator is FilterOperator.IN else (step.value,)
    if step.operator in {
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
    }:
        left = _number(value)
        right = _number(step.value)
        if left is None or right is None:
            return False
        return {
            FilterOperator.GT: left > right,
            FilterOperator.GTE: left >= right,
            FilterOperator.LT: left < right,
            FilterOperator.LTE: left <= right,
        }[step.operator]
    for candidate in candidates:
        if numeric and _is_number_value(candidate):
            if _number(value) == _number(candidate):
                return True
        elif type(value) is type(candidate) and value == candidate:
            return True
    return False


def _independent_group(
    rows: list[tuple[object, ...]],
    source_ids: list[str],
    step: GroupAggregate,
    name_indexes: dict[str, int],
    names: dict[str, str],
    source_columns: tuple[str, ...],
) -> tuple[list[tuple[object, ...]], list[tuple[str, ...]], tuple[str, ...]]:
    del source_columns
    group_indexes = tuple(name_indexes[names[column_id]] for column_id in step.group_by)
    target_index = (
        name_indexes[names[step.aggregation.column_id]]
        if step.aggregation.column_id is not None
        else None
    )
    buckets: dict[tuple[object, ...], list[tuple[tuple[object, ...], str]]] = {}
    if group_indexes:
        for row, row_id in zip(rows, source_ids, strict=True):
            key = tuple(_canonical_group_value(row[index]) for index in group_indexes)
            buckets.setdefault(key, []).append((row, row_id))
    else:
        buckets[()] = list(zip(rows, source_ids, strict=True))

    output_rows: list[tuple[object, ...]] = []
    output_lineages: list[tuple[str, ...]] = []
    output_names = tuple(names[column_id] for column_id in step.group_by) + (
        step.aggregation.output_name,
    )
    for group_key, members in buckets.items():
        keys = tuple(
            _restore_group_value(row[index] for row, _ in members)
            for index in group_indexes
        ) if group_indexes else ()
        values = (
            [row[target_index] for row, _ in members]
            if target_index is not None
            else []
        )
        aggregate = _independent_aggregate(
            values, len(members), step.aggregation.function
        )
        output_rows.append(keys + (aggregate,))
        output_lineages.append(tuple(row_id for _, row_id in members))
        del group_key
    return output_rows, output_lineages, output_names


def _restore_group_value(values) -> object:
    for value in values:
        if not _is_null(value):
            return value
    return None


def _canonical_group_value(value: object) -> object:
    if _is_null(value):
        return ("null",)
    if type(value) is bool:
        return ("number", Decimal(int(value)))
    if _is_number_value(value):
        number = _number(value)
        return ("number", number.normalize() if number is not None else Decimal(0))
    return (type(value).__name__, value)


def _independent_aggregate(
    values: list[object], row_count: int, function: AggregationFunction
) -> object:
    non_null = [value for value in values if not _is_null(value)]
    if function is AggregationFunction.COUNT:
        return len(non_null) if values else row_count
    if function is AggregationFunction.COUNT_DISTINCT:
        return len({_canonical_group_value(value) for value in non_null})
    numbers = [number for value in non_null if (number := _number(value)) is not None]
    if not numbers:
        return None
    if function is AggregationFunction.SUM:
        return _decimal_result(sum(numbers, Decimal(0)))
    if function is AggregationFunction.MEAN:
        return _decimal_result(sum(numbers, Decimal(0)) / Decimal(len(numbers)))
    if function is AggregationFunction.MIN:
        return _decimal_result(min(numbers))
    if function is AggregationFunction.MAX:
        return _decimal_result(max(numbers))
    return None


def _independent_sort_indices(
    rows: list[tuple[object, ...]],
    step: Sort,
    names: dict[str, str],
    result_columns: tuple[str, ...],
) -> list[int]:
    keys: list[tuple[int, bool]] = []
    for key in step.keys:
        name = names[key.column_id] if key.column_id is not None else key.output_name
        if name not in result_columns:
            raise ValueError("Sort key is absent from the independently derived schema.")
        keys.append((result_columns.index(name), key.ascending))

    def compare(left_index: int, right_index: int) -> int:
        left, right = rows[left_index], rows[right_index]
        for index, ascending in keys:
            left_value, right_value = left[index], right[index]
            if _is_null(left_value) or _is_null(right_value):
                result = 0 if _is_null(left_value) and _is_null(right_value) else (
                    1 if _is_null(left_value) else -1
                )
            else:
                left_key = _sort_value(left_value)
                right_key = _sort_value(right_value)
                result = (left_key > right_key) - (left_key < right_key)
                if not ascending:
                    result *= -1
            if result:
                return result
        return 0

    return sorted(range(len(rows)), key=cmp_to_key(compare))


def _result_column_names(plan, source_columns, names) -> tuple[str, ...]:
    group_step = next(
        (step for step in plan.steps if type(step) is GroupAggregate), None
    )
    if group_step is not None:
        result = tuple(names[column_id] for column_id in group_step.group_by) + (
            group_step.aggregation.output_name,
        )
    else:
        result = source_columns
    project = next((step for step in plan.steps if type(step) is Project), None)
    if project is not None:
        result = tuple(names[column_id] for column_id in project.column_ids)
        if group_step is not None:
            result += (group_step.aggregation.output_name,)
    return result


def _referenced_column_ids(plan, columns) -> tuple[str, ...]:
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
    return tuple(column.column_id for column in columns if column.column_id in used)


def _rows_equal(expected, observed, tolerance: float) -> bool:
    if len(expected) != len(observed):
        return False
    return all(
        len(expected_row) == len(observed_row)
        and all(_values_equal(left, right, tolerance) for left, right in zip(
            expected_row, observed_row, strict=True
        ))
        for expected_row, observed_row in zip(expected, observed, strict=True)
    )


def _values_equal(expected: object, observed: object, tolerance: float) -> bool:
    if _is_null(expected) or _is_null(observed):
        return _is_null(expected) and _is_null(observed)
    if _is_number_value(expected) and _is_number_value(observed):
        left, right = _number(expected), _number(observed)
        if left is None or right is None:
            return False
        threshold = Decimal(str(tolerance)) * max(
            Decimal(1), abs(left), abs(right)
        )
        return abs(left - right) <= threshold
    return type(expected) is type(observed) and expected == observed


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or _is_null(value):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _is_number_value(value: object) -> bool:
    return type(value) in {int, float, Decimal} and not (
        type(value) is float and not math.isfinite(value)
    )


def _is_null(value: object) -> bool:
    return value is None or (
        type(value) is float and math.isnan(value)
    )


def _sort_value(value: object) -> object:
    number = _number(value)
    return number if number is not None else value


def _decimal_result(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _to_result_value(value: object) -> object:
    if _is_null(value):
        return None
    return value


def _not_verified(reason: str) -> VerificationReport:
    return VerificationReport(
        status=VerificationStatus.NOT_VERIFIED,
        checks=(),
        reasons=(reason,),
        verifier_version=VERIFIER_VERSION,
    )
