"""Offline tests for deterministic trusted plan execution."""

from dataclasses import replace

import pytest

from core.executor import ExecutionError, execute_plan
from core.planner import validate_plan
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    Limit,
    OutputSpecification,
    Project,
    SelectTable,
    Sort,
    SortKey,
)


def _run(dataset, column_ids, *steps, output=None, max_result_rows=10_000):
    plan = AnalysisPlan(
        plan_id="execution-plan",
        question_id="question",
        dataset_id=dataset.manifest.dataset_id,
        steps=(SelectTable(dataset.tables[0].table_id), *steps),
        output_spec=OutputSpecification(output),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted, validation.issues
    return execute_plan(dataset, validation, max_result_rows=max_result_rows)


def test_filter_operators_and_null_behavior(
    analysis_dataset, analysis_column_ids, missing_duplicates_csv_bytes
) -> None:
    exact = _run(
        analysis_dataset,
        analysis_column_ids,
        Filter(analysis_column_ids["region"], FilterOperator.EQ, "North"),
    )
    assert exact.result.rows == (
        ("North", "100", "true"),
        ("North", "150", "true"),
    )

    greater = _run(
        analysis_dataset,
        analysis_column_ids,
        Filter(analysis_column_ids["amount"], FilterOperator.GT, 100),
    )
    assert [row[1] for row in greater.result.rows] == ["200", "150"]

    for operator, threshold, expected in (
        (FilterOperator.GTE, 150, {"150", "200"}),
        (FilterOperator.LT, 150, {"100"}),
        (FilterOperator.LTE, 150, {"100", "150"}),
    ):
        compared = _run(
            analysis_dataset,
            analysis_column_ids,
            Filter(analysis_column_ids["amount"], operator, threshold),
        )
        assert {row[1] for row in compared.result.rows} == expected

    included = _run(
        analysis_dataset,
        analysis_column_ids,
        Filter(analysis_column_ids["amount"], FilterOperator.IN, (100, 200)),
    )
    assert len(included.result.rows) == 2

    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset
    from core.verifier import verify_result

    null_dataset = ingest_upload("nulls.csv", missing_duplicates_csv_bytes)
    null_ids = {
        column.name: column.column_id
        for column in profile_dataset(null_dataset).tables[0].columns
    }
    nulls = _run(
        null_dataset,
        null_ids,
        Filter(null_ids["qty"], FilterOperator.IS_NULL),
    )
    assert nulls.result.rows == (("B", None, None),)
    distinct_non_null = _run(
        null_dataset,
        null_ids,
        GroupAggregate(
            (), Aggregation(AggregationFunction.COUNT_DISTINCT, null_ids["qty"])
        ),
    )
    assert distinct_non_null.result.rows == ((1,),)


def test_xlsx_date_values_can_be_filtered_as_typed_literals(
    multi_sheet_xlsx_bytes,
) -> None:
    from datetime import datetime

    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset
    from core.verifier import verify_result

    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    ids = {
        column.name: column.column_id
        for column in profile_dataset(dataset).tables[1].columns
    }
    plan = AnalysisPlan(
        "date-filter",
        "q",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[1].table_id),
            Filter(ids["created"], FilterOperator.EQ, datetime(2025, 1, 2)),
        ),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted
    result = execute_plan(dataset, validation)
    assert result.result.rows == (
        (True, datetime(2025, 1, 2)),
    )
    report = verify_result(dataset, validation, result)
    assert report.status.value == "verified", tuple(
        check for check in report.checks if not check.passed
    )


def test_mixed_column_filters_preserve_observed_scalar_types(
    multi_sheet_xlsx_bytes,
) -> None:
    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset
    from core.verifier import verify_result

    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    ids = {
        column.name: column.column_id
        for column in profile_dataset(dataset).tables[0].columns
    }
    plan = AnalysisPlan(
        "mixed-type-filter",
        "q",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            Filter(ids["mixed"], FilterOperator.EQ, 10),
        ),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted
    result = execute_plan(dataset, validation)
    assert result.result.rows == (("North", 10, "=1+1", 10),)
    assert verify_result(dataset, validation, result).status.value == "verified"


def test_grouped_aggregation_functions_and_null_rules(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids

    grouped_sum = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
    )
    assert grouped_sum.result.columns == ("region", "value")
    assert grouped_sum.result.rows == (("North", 250), ("South", 200))
    assert grouped_sum.result.row_lineage[0] == (
        analysis_dataset.tables[0].row_ids[0],
        analysis_dataset.tables[0].row_ids[2],
    )

    row_count = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],), Aggregation(AggregationFunction.COUNT)
        ),
    )
    assert row_count.result.rows == (("North", 2), ("South", 1))

    non_null_count = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.COUNT, ids["amount"]),
        ),
    )
    assert non_null_count.result.rows == (("North", 2), ("South", 1))

    distinct = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (), Aggregation(AggregationFunction.COUNT_DISTINCT, ids["region"])
        ),
    )
    assert distinct.result.rows == ((2,),)

    mean = _run(
        analysis_dataset,
        ids,
        GroupAggregate((), Aggregation(AggregationFunction.MEAN, ids["amount"])),
    )
    minimum = _run(
        analysis_dataset,
        ids,
        GroupAggregate((), Aggregation(AggregationFunction.MIN, ids["amount"])),
    )
    maximum = _run(
        analysis_dataset,
        ids,
        GroupAggregate((), Aggregation(AggregationFunction.MAX, ids["amount"])),
    )
    assert mean.result.rows == ((150.0,),)
    assert minimum.result.rows == ((100,),)
    assert maximum.result.rows == ((200,),)

    assert any("Null values are excluded" in warning for warning in grouped_sum.warnings)

    grouped_projection = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
        Project((ids["region"],)),
        output=("region", "value"),
    )
    assert grouped_projection.result.rows == (("North", 250), ("South", 200))


def test_sort_limit_project_empty_result_and_run_metadata(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids
    result = _run(
        analysis_dataset,
        ids,
        Sort((SortKey(ascending=False, column_id=ids["amount"]),)),
        Limit(2),
        Project((ids["region"], ids["amount"])),
        output=("region", "amount"),
    )
    assert result.result.rows == (("South", "200"), ("North", "150"))
    assert result.row_count == 2
    assert result.status == "success"
    assert result.plan_id == "execution-plan"
    assert result.run_id
    assert ids["amount"] in result.source_column_ids

    empty = _run(
        analysis_dataset,
        ids,
        Filter(ids["region"], FilterOperator.EQ, "Atlantis"),
    )
    assert empty.result.rows == ()
    assert empty.row_count == 0

    repeat = _run(
        analysis_dataset,
        ids,
        Filter(ids["region"], FilterOperator.EQ, "North"),
    )
    same = _run(
        analysis_dataset,
        ids,
        Filter(ids["region"], FilterOperator.EQ, "North"),
    )
    assert repeat.run_id == same.run_id
    assert repeat.result == same.result


def test_intermediate_sort_can_be_larger_than_bounded_final_result(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids
    result = _run(
        analysis_dataset,
        ids,
        Sort((SortKey(ascending=False, column_id=ids["amount"]),)),
        Limit(2),
        max_result_rows=2,
    )
    assert result.row_count == 2

    no_limit = AnalysisPlan(
        "over-bound",
        "q",
        analysis_dataset.manifest.dataset_id,
        (SelectTable(analysis_dataset.tables[0].table_id),),
    )
    validation = validate_plan(analysis_dataset, no_limit)
    with pytest.raises(ExecutionError, match="execution limit"):
        execute_plan(analysis_dataset, validation, max_result_rows=2)

    stable = _run(
        analysis_dataset,
        ids,
        Sort((SortKey(column_id=ids["region"]),)),
    )
    assert stable.result.row_lineage == (
        (analysis_dataset.tables[0].row_ids[0],),
        (analysis_dataset.tables[0].row_ids[2],),
        (analysis_dataset.tables[0].row_ids[1],),
    )


def test_aggregated_sort_and_bounded_result(analysis_dataset, analysis_column_ids) -> None:
    ids = analysis_column_ids
    result = _run(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
        Sort((SortKey(ascending=False, output_name="value"),)),
        Limit(1),
    )
    assert result.result.rows == (("North", 250),)
    assert result.result.row_lineage == (
        (analysis_dataset.tables[0].row_ids[0], analysis_dataset.tables[0].row_ids[2]),
    )

    plan = AnalysisPlan(
        "oversized",
        "q",
        analysis_dataset.manifest.dataset_id,
        (
            SelectTable(analysis_dataset.tables[0].table_id),
            GroupAggregate(
                (ids["region"],),
                Aggregation(AggregationFunction.COUNT),
            ),
        ),
    )
    validation = validate_plan(analysis_dataset, plan)
    with pytest.raises(ExecutionError, match="execution limit"):
        execute_plan(analysis_dataset, validation, max_result_rows=1)


def test_all_null_sum_is_null_and_count_column_is_zero(missing_duplicates_csv_bytes) -> None:
    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset

    dataset = ingest_upload("nulls.csv", missing_duplicates_csv_bytes)
    ids = {
        column.name: column.column_id
        for column in profile_dataset(dataset).tables[0].columns
    }
    empty_validation_plan = AnalysisPlan(
        "empty-sum",
        "q",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            Filter(ids["item"], FilterOperator.EQ, "B"),
            GroupAggregate((), Aggregation(AggregationFunction.SUM, ids["qty"])),
        ),
    )
    sum_validation = validate_plan(dataset, empty_validation_plan)
    assert execute_plan(dataset, sum_validation).result.rows == ((None,),)

    count_plan = replace(
        empty_validation_plan,
        plan_id="empty-count",
        steps=(
            empty_validation_plan.steps[0],
            empty_validation_plan.steps[1],
            GroupAggregate((), Aggregation(AggregationFunction.COUNT, ids["qty"])),
        ),
    )
    count_validation = validate_plan(dataset, count_plan)
    assert execute_plan(dataset, count_validation).result.rows == ((0,),)


def test_executor_requires_a_validated_safe_plan(
    analysis_dataset, analysis_column_ids
) -> None:
    hostile = "__import__('os').system('whoami')"
    plan = AnalysisPlan(
        "hostile",
        "q",
        analysis_dataset.manifest.dataset_id,
        (
            SelectTable(analysis_dataset.tables[0].table_id),
            Filter(analysis_column_ids["region"], FilterOperator.EQ, hostile),
        ),
    )
    validation = validate_plan(analysis_dataset, plan)
    assert not validation.accepted
    with pytest.raises(ExecutionError, match="accepted"):
        execute_plan(analysis_dataset, validation)
