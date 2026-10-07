"""Tests for independent recomputation and tamper detection."""

from dataclasses import replace

from core.executor import execute_plan
from core.planner import validate_plan
from core.verifier import verify_result
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    Limit,
    OutputSpecification,
    SelectTable,
    Sort,
    SortKey,
    VerificationStatus,
)


def _execute(dataset, ids, *steps, output=None):
    plan = AnalysisPlan(
        "verify-plan",
        "verify-question",
        dataset.manifest.dataset_id,
        (SelectTable(dataset.tables[0].table_id), *steps),
        OutputSpecification(output),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted, validation.issues
    return validation, execute_plan(dataset, validation)


def _tamper_rows(result, rows, columns=None, lineage=None):
    new_table = replace(
        result.result,
        rows=tuple(rows),
        columns=result.result.columns if columns is None else tuple(columns),
        row_lineage=(
            result.result.row_lineage if lineage is None else tuple(lineage)
        ),
    )
    return replace(result, result=new_table, row_count=len(new_table.rows))


def test_correct_aggregate_is_verified(analysis_dataset, analysis_column_ids) -> None:
    validation, result = _execute(
        analysis_dataset,
        analysis_column_ids,
        GroupAggregate(
            (analysis_column_ids["region"],),
            Aggregation(AggregationFunction.SUM, analysis_column_ids["amount"]),
        ),
    )

    report = verify_result(analysis_dataset, validation, result)

    assert report.status is VerificationStatus.VERIFIED
    assert {check.name for check in report.checks} >= {
        "result_schema",
        "row_count",
        "group_keys",
        "aggregate_values",
        "row_lineage",
    }
    assert "does not establish source-data truth" in report.reasons[0]


def test_tampered_aggregate_filter_group_sort_limit_and_schema_fail(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids
    validation, result = _execute(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
        Sort((SortKey(ascending=False, output_name="value"),)),
        Limit(1),
    )
    tampered_aggregate = _tamper_rows(
        result,
        (("North", 999),),
        lineage=(result.result.row_lineage[0],),
    )
    assert (
        verify_result(analysis_dataset, validation, tampered_aggregate).status
        is VerificationStatus.FAILED
    )

    filter_validation, filtered = _execute(
        analysis_dataset,
        ids,
        Filter(ids["region"], FilterOperator.EQ, "North"),
    )
    tampered_filter = _tamper_rows(
        filtered,
        (("South", "200", "false"),),
        lineage=(filtered.result.row_lineage[0],),
    )
    assert (
        verify_result(analysis_dataset, filter_validation, tampered_filter).status
        is VerificationStatus.FAILED
    )

    group_validation, grouped = _execute(
        analysis_dataset,
        ids,
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.COUNT),
        ),
    )
    tampered_group = _tamper_rows(
        grouped,
        (("North", 1), ("South", 2)),
        lineage=grouped.result.row_lineage,
    )
    assert (
        verify_result(analysis_dataset, group_validation, tampered_group).status
        is VerificationStatus.FAILED
    )

    sort_validation, sorted_result = _execute(
        analysis_dataset,
        ids,
        Sort((SortKey(ascending=False, column_id=ids["amount"]),)),
        Limit(2),
    )
    tampered_sort = _tamper_rows(
        sorted_result,
        tuple(reversed(sorted_result.result.rows)),
        lineage=tuple(reversed(sorted_result.result.row_lineage)),
    )
    assert (
        verify_result(analysis_dataset, sort_validation, tampered_sort).status
        is VerificationStatus.FAILED
    )
    too_many_rows = _tamper_rows(
        sorted_result,
        sorted_result.result.rows + (("North", "100", "true"),),
        lineage=sorted_result.result.row_lineage
        + ((analysis_dataset.tables[0].row_ids[0],),),
    )
    too_many_report = verify_result(analysis_dataset, sort_validation, too_many_rows)
    assert too_many_report.status is VerificationStatus.FAILED
    assert any(
        check.name == "limit" and not check.passed
        for check in too_many_report.checks
    )

    tampered_schema = _tamper_rows(
        sorted_result,
        sorted_result.result.rows,
        columns=("wrong", "schema", "here"),
    )
    assert (
        verify_result(analysis_dataset, sort_validation, tampered_schema).status
        is VerificationStatus.FAILED
    )


def test_numeric_tolerance_and_partial_verification(
    analysis_dataset, analysis_column_ids
) -> None:
    validation, result = _execute(
        analysis_dataset,
        analysis_column_ids,
        GroupAggregate(
            (),
            Aggregation(AggregationFunction.MEAN, analysis_column_ids["amount"]),
        ),
    )
    close_result = _tamper_rows(result, ((150.0 + 5e-10,),))
    assert (
        verify_result(analysis_dataset, validation, close_result).status
        is VerificationStatus.VERIFIED
    )
    assert (
        verify_result(analysis_dataset, validation, close_result, tolerance=0).status
        is VerificationStatus.FAILED
    )

    no_lineage = _tamper_rows(
        result,
        result.result.rows,
        lineage=((),),
    )
    report = verify_result(analysis_dataset, validation, no_lineage)
    assert report.status is VerificationStatus.PARTIALLY_VERIFIED
    assert any("lineage was not provided" in reason for reason in report.reasons)


def test_unavailable_validation_is_not_verified(analysis_dataset) -> None:
    report = verify_result(analysis_dataset, None, None)
    assert report.status is VerificationStatus.NOT_VERIFIED
    assert not report.checks


def test_verifier_does_not_call_executor(
    analysis_dataset, analysis_column_ids, monkeypatch
) -> None:
    validation, result = _execute(
        analysis_dataset,
        analysis_column_ids,
        Filter(analysis_column_ids["region"], FilterOperator.EQ, "North"),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("verifier must not call the executor")

    monkeypatch.setattr("core.executor.execute_plan", forbidden)
    report = verify_result(analysis_dataset, validation, result)

    assert report.status is VerificationStatus.VERIFIED


def test_null_group_key_and_empty_filter_verify_independently() -> None:
    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset

    dataset = ingest_upload(
        "nullable.csv",
        b"team,amount\nA,2\n,3\nA,4\n",
    )
    ids = {
        column.name: column.column_id
        for column in profile_dataset(dataset).tables[0].columns
    }
    validation, grouped = _execute(
        dataset,
        ids,
        GroupAggregate(
            (ids["team"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
    )
    assert verify_result(dataset, validation, grouped).status is VerificationStatus.VERIFIED

    empty_validation, empty_result = _execute(
        dataset,
        ids,
        Filter(ids["team"], FilterOperator.EQ, "missing"),
    )
    assert empty_result.result.rows == ()
    assert (
        verify_result(dataset, empty_validation, empty_result).status
        is VerificationStatus.VERIFIED
    )
