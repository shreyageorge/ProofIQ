"""Bounded, immutable evidence manifest tests."""

import json

import pytest

from core.evidence import (
    build_evidence,
    evidence_to_dict,
    serialize_evidence,
)
from core.executor import execute_plan
from core.ingestion import ingest_upload
from core.planner import validate_plan
from core.profiler import profile_dataset
from core.verifier import verify_result
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    GroupAggregate,
    Limit,
    QuestionRequest,
    SelectTable,
    Sort,
    SortKey,
    VerificationReport,
    VerificationStatus,
)


def _verified_evidence(dataset, ids):
    plan = AnalysisPlan(
        "evidence-plan",
        "evidence-question",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            GroupAggregate(
                (ids["region"],),
                Aggregation(AggregationFunction.SUM, ids["amount"]),
            ),
            Sort((SortKey(ascending=False, output_name="value"),)),
            Limit(1),
        ),
        assumptions=("CSV numeric strings are interpreted numerically.",),
    )
    validation = validate_plan(dataset, plan)
    assert validation.accepted, validation.issues
    execution = execute_plan(dataset, validation)
    verification = verify_result(dataset, validation, execution)
    request = QuestionRequest(
        "evidence-question",
        "What is the largest regional total?",
        dataset.manifest.dataset_id,
    )
    return build_evidence(request, dataset, validation, execution, verification)


def test_evidence_references_source_and_records_operations(
    analysis_dataset, analysis_column_ids
) -> None:
    evidence = _verified_evidence(analysis_dataset, analysis_column_ids)

    assert evidence.dataset_sha256 == analysis_dataset.manifest.content_sha256
    assert len(evidence.source_tables) == 1
    assert evidence.source_tables[0].table_id == analysis_dataset.tables[0].table_id
    assert {column.name for column in evidence.source_columns} == {"region", "amount"}
    assert [operation.kind.value for operation in evidence.operations] == [
        "group_aggregate",
        "sort",
        "limit",
    ]
    assert len(evidence.calculations) == 1
    assert len(evidence.transformations) == 2
    assert evidence.result is not None
    assert evidence.result.row_count == 1
    assert evidence.result.content_sha256 == evidence.execution.result_sha256
    assert evidence.result.row_lineage == (
        (
            analysis_dataset.tables[0].row_ids[0],
            analysis_dataset.tables[0].row_ids[2],
        ),
    )
    assert evidence.result.preview_rows == (("North", 250),)
    assert not evidence.result.preview_truncated
    assert evidence.verification.status is VerificationStatus.VERIFIED
    assert evidence.assumptions == (
        "CSV numeric strings are interpreted numerically.",
    )
    assert "does not establish source-data truth" in evidence.limitations[0]


def test_evidence_serialization_is_deterministic_and_does_not_copy_full_results() -> None:
    csv = "region,amount\n" + "".join(
        f"Region{index},{index}\n" for index in range(30)
    )
    dataset = ingest_upload("many-regions.csv", csv.encode())
    ids = {
        column.name: column.column_id
        for column in profile_dataset(dataset).tables[0].columns
    }
    plan = AnalysisPlan(
        "bounded-evidence-plan",
        "bounded-evidence-question",
        dataset.manifest.dataset_id,
        (
            SelectTable(dataset.tables[0].table_id),
            GroupAggregate(
                (ids["region"],),
                Aggregation(AggregationFunction.COUNT),
            ),
        ),
    )
    validation = validate_plan(dataset, plan)
    execution = execute_plan(dataset, validation)
    verification = verify_result(dataset, validation, execution)
    request = QuestionRequest(
        "bounded-evidence-question",
        "Count rows by region.",
        dataset.manifest.dataset_id,
    )

    first = build_evidence(request, dataset, validation, execution, verification)
    second = build_evidence(request, dataset, validation, execution, verification)
    serialized = serialize_evidence(first)

    assert serialized == serialize_evidence(second)
    assert json.loads(serialized) == evidence_to_dict(first)
    assert "Region29" not in serialized
    assert first.source_row_count == 30
    assert first.result is not None and first.result.row_count == 30
    assert first.result.preview_truncated
    values_check = next(
        check
        for check in first.verification.checks
        if check.name == "result_values"
    )
    assert values_check.preview_truncated
    assert len(values_check.expected_preview) <= 5
    assert not hasattr(first, "source_rows")
    with pytest.raises(TypeError, match="Only an EvidenceRecord"):
        evidence_to_dict(object())


def test_failed_execution_is_explicitly_recorded(analysis_dataset):
    validation = validate_plan(
        analysis_dataset,
        AnalysisPlan(
            "failed-plan",
            "failed-question",
            analysis_dataset.manifest.dataset_id,
            (SelectTable(analysis_dataset.tables[0].table_id),),
        ),
    )
    request = QuestionRequest(
        "failed-question", "Show the table.", analysis_dataset.manifest.dataset_id
    )
    unavailable = VerificationReport(
        VerificationStatus.NOT_VERIFIED,
        (),
        ("Execution was unavailable.",),
        "1.0",
    )

    evidence = build_evidence(
        request,
        analysis_dataset,
        validation,
        None,
        unavailable,
        execution_error="Execution failed before producing a result.",
    )

    assert evidence.result is None
    assert evidence.execution.status == "failed"
    assert evidence.execution.error is not None
    assert evidence.verification.status is VerificationStatus.NOT_VERIFIED
    with pytest.raises(ValueError, match="explicit error"):
        build_evidence(request, analysis_dataset, validation, None, unavailable)
