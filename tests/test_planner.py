"""Strict typed-plan validation tests."""

from dataclasses import replace

from core.answerability import assess_answerability
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
    QuestionIntent,
    SelectTable,
    Sort,
    SortKey,
)


def _plan(dataset, steps, output_columns=None):
    return AnalysisPlan(
        plan_id="plan-1",
        question_id="question-1",
        dataset_id=dataset.manifest.dataset_id,
        steps=tuple(steps),
        output_spec=OutputSpecification(output_columns),
    )


def test_valid_plan_is_accepted(analysis_dataset, analysis_column_ids) -> None:
    table_id = analysis_dataset.tables[0].table_id
    plan = _plan(
        analysis_dataset,
        (
            SelectTable(table_id),
            Filter(analysis_column_ids["amount"], FilterOperator.GT, 100),
            Sort((SortKey(ascending=False, column_id=analysis_column_ids["amount"]),)),
            Limit(2),
            Project((analysis_column_ids["region"], analysis_column_ids["amount"])),
        ),
        ("region", "amount"),
    )

    validation = validate_plan(analysis_dataset, plan)

    assert validation.accepted
    assert validation.validated_plan == plan
    assert not validation.issues


def test_nonexistent_table_and_column_are_rejected(
    analysis_dataset,
) -> None:
    unknown_table = _plan(analysis_dataset, (SelectTable("missing-table"),))
    assert not validate_plan(analysis_dataset, unknown_table).accepted

    unknown_column = _plan(
        analysis_dataset,
        (
            SelectTable(analysis_dataset.tables[0].table_id),
            Filter("missing-column", FilterOperator.EQ, "North"),
        ),
    )
    assert not validate_plan(analysis_dataset, unknown_column).accepted


def test_bad_operator_incompatible_aggregation_and_invalid_limit_are_rejected(
    analysis_dataset, analysis_column_ids
) -> None:
    table_id = analysis_dataset.tables[0].table_id
    invalid_operator = _plan(
        analysis_dataset,
        (SelectTable(table_id), Filter(analysis_column_ids["amount"], "exec", 1)),
    )
    assert not validate_plan(analysis_dataset, invalid_operator).accepted

    incompatible = _plan(
        analysis_dataset,
        (
            SelectTable(table_id),
            GroupAggregate(
                (),
                Aggregation(AggregationFunction.SUM, analysis_column_ids["region"]),
            ),
        ),
    )
    assert not validate_plan(analysis_dataset, incompatible).accepted

    invalid_limit = _plan(analysis_dataset, (SelectTable(table_id), Limit(0)))
    assert not validate_plan(analysis_dataset, invalid_limit).accepted

    numeric_text_literal = _plan(
        analysis_dataset,
        (SelectTable(table_id), Filter(analysis_column_ids["amount"], FilterOperator.EQ, "100")),
    )
    assert not validate_plan(analysis_dataset, numeric_text_literal).accepted

    over_limit = _plan(
        analysis_dataset,
        (SelectTable(table_id), Limit(10_001)),
    )
    assert not validate_plan(analysis_dataset, over_limit).accepted


def test_excessive_steps_unsupported_join_and_malformed_plan_are_rejected(
    analysis_dataset, analysis_column_ids
) -> None:
    table_id = analysis_dataset.tables[0].table_id
    too_many_steps = _plan(
        analysis_dataset,
        (SelectTable(table_id),)
        + tuple(
            Filter(analysis_column_ids["region"], FilterOperator.EQ, "North")
            for _ in range(3)
        ),
    )
    assert not validate_plan(analysis_dataset, too_many_steps, max_steps=3).accepted

    unsupported_join = _plan(analysis_dataset, (SelectTable(table_id), object()))
    assert not validate_plan(analysis_dataset, unsupported_join).accepted

    malformed = replace(too_many_steps, steps=[SelectTable(table_id)])
    assert not validate_plan(analysis_dataset, malformed).accepted


def test_expression_like_values_are_rejected_as_data(
    analysis_dataset, analysis_column_ids
) -> None:
    for hostile in (
        "__import__('os').system('whoami')",
        "sum(amount)",
    ):
        plan = _plan(
            analysis_dataset,
            (
                SelectTable(analysis_dataset.tables[0].table_id),
                Filter(analysis_column_ids["region"], FilterOperator.EQ, hostile),
            ),
        )

        validation = validate_plan(analysis_dataset, plan)

        assert not validation.accepted
        assert any("Expression-like" in issue for issue in validation.issues)


def test_ambiguous_column_name_is_not_guessed() -> None:
    from core.ingestion import ingest_upload

    dataset = ingest_upload(
        "ambiguous.csv",
        b"Region,region,amount\nNorth,North,2\n",
    )
    intent = QuestionIntent(
        question_id="question-1",
        dataset_id=dataset.manifest.dataset_id,
        required_fields=("region",),
    )

    result = assess_answerability(dataset, intent)

    assert result.status.value == "needs_clarification"
    assert "region" in result.missing_fields


def test_plan_dataset_and_step_order_are_validated(analysis_dataset, analysis_column_ids):
    table_id = analysis_dataset.tables[0].table_id
    wrong_dataset = replace(
        _plan(analysis_dataset, (SelectTable(table_id),)),
        dataset_id="another-dataset",
    )
    assert not validate_plan(analysis_dataset, wrong_dataset).accepted

    invalid_order = _plan(
        analysis_dataset,
        (
            SelectTable(table_id),
            GroupAggregate(
                (),
                Aggregation(AggregationFunction.SUM, analysis_column_ids["amount"]),
            ),
            Filter(analysis_column_ids["region"], FilterOperator.EQ, "North"),
        ),
    )
    assert not validate_plan(analysis_dataset, invalid_order).accepted


def test_column_id_from_another_table_is_rejected(
    multi_sheet_xlsx_bytes,
) -> None:
    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset

    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    profiles = profile_dataset(dataset).tables
    other_table_column_id = profiles[1].columns[0].column_id
    plan = AnalysisPlan(
        "foreign-column",
        "q",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            Filter(other_table_column_id, FilterOperator.EQ, True),
        ),
    )

    assert not validate_plan(dataset, plan).accepted


def test_numeric_identifier_is_not_assumed_to_be_a_measure() -> None:
    from core.ingestion import ingest_upload
    from core.profiler import profile_dataset

    dataset = ingest_upload("ids.csv", b"customer_id\n001\n002\n")
    column_id = profile_dataset(dataset).tables[0].columns[0].column_id
    plan = AnalysisPlan(
        "sum-ids",
        "q",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            GroupAggregate((), Aggregation(AggregationFunction.SUM, column_id)),
        ),
    )

    assert not validate_plan(dataset, plan).accepted
