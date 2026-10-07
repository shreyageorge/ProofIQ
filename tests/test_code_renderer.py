"""Display-only source rendering tests."""

from datetime import datetime

import pytest

from core.code_renderer import format_python_literal, render_plan_code
from core.planner import validate_plan
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    Limit,
    PlanValidation,
    Project,
    SelectTable,
    Sort,
    SortKey,
)


def _validated_plan(dataset, ids, *steps):
    plan = AnalysisPlan(
        "render-plan",
        "render-question",
        dataset.manifest.dataset_id,
        (SelectTable(dataset.tables[0].table_id), *steps),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted, validation.issues
    return validation


def test_renderer_is_deterministic_and_shows_only_supported_steps(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids
    validation = _validated_plan(
        analysis_dataset,
        ids,
        Filter(ids["region"], FilterOperator.EQ, "North"),
        GroupAggregate(
            (ids["region"],),
            Aggregation(AggregationFunction.SUM, ids["amount"]),
        ),
        Sort((SortKey(ascending=False, output_name="value"),)),
        Limit(1),
        Project((ids["region"],)),
    )

    rendered = render_plan_code(analysis_dataset, validation)

    assert rendered == render_plan_code(analysis_dataset, validation)
    assert "Generated for review only" in rendered
    assert f"tables[{analysis_dataset.tables[0].table_id!r}].copy()" in rendered
    assert "== 'North'" in rendered
    assert "dropna=False, sort=False, observed=True" in rendered
    assert "min_count=1" in rendered
    assert "kind='mergesort', na_position='last'" in rendered
    assert "_df = _df.head(1)" in rendered
    assert "_df = _df.loc[:, ['region', 'value']]" in rendered
    assert "result = _df" in rendered


def test_renderer_quotes_malicious_values_as_data_and_rejects_invalid_plans(
    analysis_dataset, analysis_column_ids
) -> None:
    hostile = "__import__('os').system('whoami')"
    assert format_python_literal(hostile) == repr(hostile)
    assert format_python_literal((hostile,)) == repr((hostile,))

    plan = AnalysisPlan(
        "hostile-plan",
        "q",
        analysis_dataset.manifest.dataset_id,
        (
            SelectTable(analysis_dataset.tables[0].table_id),
            Filter(analysis_column_ids["region"], FilterOperator.EQ, hostile),
        ),
    )
    rejected = validate_plan(analysis_dataset, plan)
    assert not rejected.accepted
    with pytest.raises(ValueError, match="accepted validated plan"):
        render_plan_code(analysis_dataset, rejected)

    forged_validation = PlanValidation(True, plan)
    with pytest.raises(ValueError, match="not valid"):
        render_plan_code(analysis_dataset, forged_validation)


def test_python_literals_are_type_limited_and_date_safe() -> None:
    assert format_python_literal(datetime(2025, 1, 2)) == (
        "datetime.fromisoformat('2025-01-02T00:00:00')"
    )
    with pytest.raises(ValueError, match="Unsupported plan literal"):
        format_python_literal(object())
