"""Deterministic, display-only Pandas code rendering from validated plans."""

from __future__ import annotations

import math
from datetime import date, datetime, time

from core.planner import validate_plan
from core.profiler import profile_dataset
from models.schemas import (
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    IngestedDataset,
    Limit,
    PlanValidation,
    Project,
    SelectTable,
    Sort,
)

CODE_RENDERER_VERSION = "1.0"


def render_plan_code(
    dataset: IngestedDataset, validation: PlanValidation
) -> str:
    """Render reviewable Python for an accepted plan; never execute generated code."""
    if (
        type(validation) is not PlanValidation
        or not validation.accepted
        or type(validation.validated_plan) is not AnalysisPlan
    ):
        raise ValueError("Code rendering requires an accepted validated plan.")
    plan = validation.validated_plan
    current = validate_plan(dataset, plan)
    if not current.accepted or current.validated_plan != plan:
        raise ValueError("The plan is not valid for the supplied dataset.")

    table_id = plan.steps[0].table_id
    profile = next(
        profile
        for profile in profile_dataset(dataset).tables
        if profile.table_id == table_id
    )
    column_names = {column.column_id: column.name for column in profile.columns}
    numeric_ids = {
        column.column_id
        for column in profile.columns
        if any(hint.value == "numeric" for hint in column.semantic_hints)
    }
    mixed_ids = {
        column.column_id
        for column in profile.columns
        if column.physical_type.value == "mixed"
    }

    lines = [
        "# Generated for review only; ProofIQ does not execute this source.",
        "import pandas as pd",
        "from datetime import date, datetime, time",
        "",
        f"_df = tables[{format_python_literal(table_id)}].copy().reset_index(drop=True)",
    ]
    filter_index = 0
    for step in plan.steps[1:]:
        if type(step) is Filter:
            lines.extend(
                _render_filter(
                    step,
                    column_names,
                    numeric_ids,
                    mixed_ids,
                    filter_index,
                )
            )
            filter_index += 1
        elif type(step) is GroupAggregate:
            lines.extend(_render_group(step, column_names))
        elif type(step) is Sort:
            lines.extend(_render_sort(step, column_names, numeric_ids))
        elif type(step) is Limit:
            lines.append(f"_df = _df.head({step.count})")
        elif type(step) is Project:
            projection = [column_names[column_id] for column_id in step.column_ids]
            group = next(
                (
                    candidate
                    for candidate in plan.steps
                    if type(candidate) is GroupAggregate
                ),
                None,
            )
            if group is not None:
                projection.append(group.aggregation.output_name)
            lines.append(f"_df = _df.loc[:, {_literal_list(projection)}]")
        elif type(step) is SelectTable:
            raise ValueError("A plan may select a table only in its first step.")
        else:
            raise ValueError("The validated plan contains an unsupported step.")
    lines.extend(("", "result = _df"))
    return "\n".join(lines)


def format_python_literal(value: object) -> str:
    """Return a literal expression for a supported scalar value, never raw code."""
    if value is None or type(value) in {str, bool, int}:
        return repr(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Only finite floating-point literals can be rendered.")
        return repr(value)
    if type(value) is datetime:
        return f"datetime.fromisoformat({value.isoformat()!r})"
    if type(value) is date:
        return f"date.fromisoformat({value.isoformat()!r})"
    if type(value) is time:
        return f"time.fromisoformat({value.isoformat()!r})"
    if isinstance(value, tuple):
        if any(
            item is not None
            and type(item) not in {str, bool, int, float, date, datetime, time}
            for item in value
        ):
            raise ValueError("Only scalar tuple members can be rendered.")
        return "(" + ", ".join(format_python_literal(item) for item in value) + (
            "," if len(value) == 1 else ""
        ) + ")"
    raise ValueError(f"Unsupported plan literal type: {type(value).__name__}.")


def _render_filter(step, names, numeric_ids, mixed_ids, index: int) -> list[str]:
    column = format_python_literal(names[step.column_id])
    mask_name = f"_mask_{index}"
    if step.operator is FilterOperator.IS_NULL:
        expression = f"_df.loc[:, {column}].isna()"
    elif step.column_id in mixed_ids:
        values = (
            step.value
            if step.operator is FilterOperator.IN
            else (step.value,)
        )
        expression = (
            f"_df.loc[:, {column}].map("
            "lambda observed: not pd.isna(observed) and any("
            "type(observed) is type(candidate) and observed == candidate "
            f"for candidate in {format_python_literal(values)}))"
        )
    else:
        source = f"_df.loc[:, {column}]"
        if (
            step.column_id in numeric_ids
            and step.operator in {FilterOperator.EQ, FilterOperator.IN}
        ):
            source = f"pd.to_numeric({source}, errors='coerce')"
        operator = {
            FilterOperator.EQ: "==",
            FilterOperator.IN: ".isin",
            FilterOperator.GT: ">",
            FilterOperator.GTE: ">=",
            FilterOperator.LT: "<",
            FilterOperator.LTE: "<=",
        }.get(step.operator)
        if operator is None:
            raise ValueError(f"Unsupported filter operator: {step.operator!r}.")
        if step.operator is FilterOperator.IN:
            values = format_python_literal(step.value)
            expression = f"{source}.isin({values})"
        else:
            expression = f"{source} {operator} {format_python_literal(step.value)}"
    return [
        f"{mask_name} = {expression}",
        f"_df = _df.loc[{mask_name}.fillna(False)]",
    ]


def _render_group(step: GroupAggregate, names: dict[str, str]) -> list[str]:
    group_names = [names[column_id] for column_id in step.group_by]
    group_columns = _literal_list(group_names)
    aggregation = step.aggregation
    output = format_python_literal(aggregation.output_name)
    function = aggregation.function

    if not group_names:
        value = _render_scalar_aggregation(step, names)
        return [f"_df = pd.DataFrame({{{output}: [{value}]}})"]

    lines = [
        f"_grouped = _df.groupby({group_columns}, dropna=False, sort=False, observed=True)"
    ]
    if function is AggregationFunction.COUNT and aggregation.column_id is None:
        lines.append(
            f"_df = _grouped.size().rename({output}).reset_index()"
        )
        return lines

    column_name = names[aggregation.column_id]
    reducer = _render_reducer(function, column_name)
    lines.append(
        "_df = _grouped.agg("
        f"**{{{output}: ({format_python_literal(column_name)}, {reducer})}}"
        ").reset_index()"
    )
    return lines


def _render_scalar_aggregation(step: GroupAggregate, names: dict[str, str]) -> str:
    aggregation = step.aggregation
    function = aggregation.function
    if function is AggregationFunction.COUNT and aggregation.column_id is None:
        return "len(_df.index)"
    column_name = names[aggregation.column_id]
    series = f"_df.loc[:, {format_python_literal(column_name)}]"
    return _render_series_aggregate(function, series)


def _render_reducer(function: AggregationFunction, column_name: str) -> str:
    series = "values"
    if function in {
        AggregationFunction.SUM,
        AggregationFunction.MEAN,
        AggregationFunction.MIN,
        AggregationFunction.MAX,
    }:
        series = "pd.to_numeric(values, errors='coerce')"
    return f"lambda values: {_render_series_aggregate(function, series)}"


def _render_series_aggregate(function: AggregationFunction, series: str) -> str:
    if function is AggregationFunction.COUNT:
        return f"{series}.count()"
    if function is AggregationFunction.COUNT_DISTINCT:
        return f"{series}.nunique(dropna=True)"
    if function is AggregationFunction.SUM:
        return f"{series}.sum(min_count=1)"
    if function is AggregationFunction.MEAN:
        return f"{series}.mean()"
    if function is AggregationFunction.MIN:
        return f"{series}.min()"
    if function is AggregationFunction.MAX:
        return f"{series}.max()"
    raise ValueError(f"Unsupported aggregation function: {function!r}.")


def _render_sort(step: Sort, names: dict[str, str], numeric_ids: set[str]) -> list[str]:
    lines = ["_sort_columns = []"]
    ascending = []
    for index, key in enumerate(step.keys):
        column_name = (
            names[key.column_id] if key.column_id is not None else key.output_name
        )
        sort_name = f"_sort_key_{index}"
        if key.column_id is not None and key.column_id in numeric_ids:
            lines.append(
                f"{sort_name} = pd.to_numeric("
                f"_df.loc[:, {format_python_literal(column_name)}], errors='coerce')"
            )
        else:
            lines.append(f"{sort_name} = _df.loc[:, {format_python_literal(column_name)}]")
        lines.append(f"_sort_columns.append({sort_name})")
        ascending.append(key.ascending)
    lines.append(
        "_df = _df.loc[_df.assign(**{"
        + ", ".join(
            f"'_proofiq_sort_{index}': _sort_columns[{index}]"
            for index in range(len(step.keys))
        )
        + "}).sort_values("
        + f"by={_literal_list([f'_proofiq_sort_{index}' for index in range(len(step.keys))])}, "
        + f"ascending={format_python_literal(tuple(ascending))}, "
        + "kind='mergesort', na_position='last').index]"
    )
    return lines


def _literal_list(values: list[str]) -> str:
    return "[" + ", ".join(format_python_literal(value) for value in values) + "]"
