"""Immutable evidence records for reproducible, verified analysis runs."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum

from core.code_renderer import CODE_RENDERER_VERSION, render_plan_code
from core.planner import validate_plan
from core.profiler import profile_dataset
from core.schema import stable_id
from models.schemas import (
    AnalysisPlan,
    CellValue,
    ExecutionResult,
    ExecutionStatus,
    Filter,
    GroupAggregate,
    IngestedDataset,
    Limit,
    PlanValidation,
    Project,
    QuestionRequest,
    SelectTable,
    Sort,
    VerificationReport,
    VerificationStatus,
)

EVIDENCE_VERSION = "1.0"
EXECUTOR_VERSION = "1.0"
MAX_EVIDENCE_PREVIEW_ROWS = 5
MAX_EVIDENCE_PREVIEW_COLUMNS = 20


class EvidenceOperationKind(str, Enum):
    FILTER = "filter"
    GROUP_AGGREGATE = "group_aggregate"
    SORT = "sort"
    LIMIT = "limit"
    PROJECT = "project"


@dataclass(frozen=True)
class EvidenceTable:
    table_id: str
    display_name: str
    source_file_id: str
    sheet_name: str | None
    row_count: int
    column_count: int


@dataclass(frozen=True)
class EvidenceColumn:
    column_id: str
    table_id: str
    name: str
    index: int
    source_file_id: str
    sheet_name: str | None


@dataclass(frozen=True)
class OperationReference:
    step_index: int
    kind: EvidenceOperationKind
    column_ids: tuple[str, ...] = ()
    output_name: str | None = None
    operator: str | None = None
    value: CellValue | tuple[CellValue, ...] | None = None
    aggregation: str | None = None
    limit: int | None = None
    ascending: tuple[bool, ...] = ()
    output_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionReference:
    run_id: str | None
    plan_id: str
    status: str
    row_count: int | None
    warnings: tuple[str, ...]
    duration_ms: float | None
    source_column_ids: tuple[str, ...]
    result_columns: tuple[str, ...]
    result_sha256: str | None
    error: str | None = None


@dataclass(frozen=True)
class ResultReference:
    columns: tuple[str, ...]
    row_count: int
    content_sha256: str
    lineage_row_count: int
    row_lineage: tuple[tuple[str, ...], ...]
    preview_rows: tuple[tuple[object, ...], ...]
    preview_truncated: bool


@dataclass(frozen=True)
class VerificationCheckReference:
    name: str
    passed: bool
    expected_preview: object
    observed_preview: object
    tolerance: float | None
    detail: str
    preview_truncated: bool


@dataclass(frozen=True)
class VerificationReference:
    status: VerificationStatus
    checks: tuple[VerificationCheckReference, ...]
    reasons: tuple[str, ...]
    verifier_version: str


@dataclass(frozen=True)
class ComponentVersions:
    evidence: str
    plan: int
    executor: str
    verifier: str
    code_renderer: str


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    version: str
    question_id: str
    question: str
    dataset_id: str
    dataset_sha256: str
    plan: AnalysisPlan
    source_tables: tuple[EvidenceTable, ...]
    source_columns: tuple[EvidenceColumn, ...]
    operations: tuple[OperationReference, ...]
    filters: tuple[OperationReference, ...]
    transformations: tuple[OperationReference, ...]
    calculations: tuple[OperationReference, ...]
    source_row_count: int
    result: ResultReference | None
    execution: ExecutionReference
    verification: VerificationReference
    generated_code: str
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]
    component_versions: ComponentVersions


def build_evidence(
    request: QuestionRequest,
    dataset: IngestedDataset,
    validation: PlanValidation,
    execution: ExecutionResult | None,
    verification: VerificationReport,
    *,
    execution_error: str | None = None,
    limitations: tuple[str, ...] = (),
) -> EvidenceRecord:
    """Create an immutable manifest linked to the validated plan and source snapshot."""
    if (
        type(request) is not QuestionRequest
        or type(dataset) is not IngestedDataset
        or type(validation) is not PlanValidation
        or not validation.accepted
        or type(validation.validated_plan) is not AnalysisPlan
        or type(verification) is not VerificationReport
    ):
        raise ValueError("Evidence requires a question, dataset, validated plan, and verification.")
    plan = validation.validated_plan
    if (
        request.question_id != plan.question_id
        or request.dataset_id != dataset.manifest.dataset_id
        or plan.dataset_id != dataset.manifest.dataset_id
    ):
        raise ValueError("Evidence inputs do not refer to the same question and dataset.")
    confirmed = validate_plan(dataset, plan)
    if not confirmed.accepted or confirmed.validated_plan != plan:
        raise ValueError("The analysis plan is not valid for the supplied dataset.")
    if execution is not None:
        if (
            type(execution) is not ExecutionResult
            or execution.status is not ExecutionStatus.SUCCESS
            or execution.plan_id != plan.plan_id
            or execution.result is None
            or execution.row_count != len(execution.result.rows)
            or execution.run_id
            != stable_id(
                "run", f"{dataset.manifest.content_sha256}\0{repr(plan)}"
            )
        ):
            raise ValueError("The execution result does not match the validated plan.")
        if execution_error is not None:
            raise ValueError("A successful execution cannot include an execution error.")
    elif not isinstance(execution_error, str) or not execution_error.strip():
        raise ValueError("A failed or unavailable execution requires an explicit error.")
    elif verification.status is not VerificationStatus.NOT_VERIFIED:
        raise ValueError("An unavailable execution must have NOT_VERIFIED status.")
    if not isinstance(limitations, tuple) or any(
        not isinstance(item, str) for item in limitations
    ):
        raise ValueError("Limitations must be a tuple of strings.")

    table_id = plan.steps[0].table_id
    source = next(table for table in dataset.tables if table.table_id == table_id)
    table_ref = next(ref for ref in dataset.manifest.tables if ref.table_id == table_id)
    profile = next(
        table for table in profile_dataset(dataset).tables if table.table_id == table_id
    )
    operations = _operation_references(plan)
    referenced_ids = set()
    for operation in operations:
        referenced_ids.update(operation.column_ids)
    if execution is not None:
        referenced_ids.update(execution.source_column_ids)
    source_columns = tuple(
        EvidenceColumn(
            column_id=column.column_id,
            table_id=table_id,
            name=column.name,
            index=index,
            source_file_id=table_ref.source_file_id,
            sheet_name=table_ref.source_sheet,
        )
        for index, column in enumerate(profile.columns)
        if column.column_id in referenced_ids
    )
    source_tables = (
        EvidenceTable(
            table_id=table_id,
            display_name=table_ref.display_name,
            source_file_id=table_ref.source_file_id,
            sheet_name=table_ref.source_sheet,
            row_count=len(source.rows),
            column_count=len(source.columns),
        ),
    )

    result_ref = None
    result_columns: tuple[str, ...] = ()
    result_sha256 = None
    result_row_count = None
    lineage_count = 0
    run_id = None
    warnings: tuple[str, ...] = ()
    duration_ms = None
    execution_source_columns: tuple[str, ...] = ()
    status = "failed"
    if execution is not None:
        result_columns = execution.result.columns
        result_row_count = execution.row_count
        result_sha256 = _result_digest(execution)
        lineage_count = sum(bool(ids) for ids in execution.result.row_lineage)
        result_ref = ResultReference(
            columns=result_columns,
            row_count=result_row_count,
            content_sha256=result_sha256,
            lineage_row_count=lineage_count,
            row_lineage=execution.result.row_lineage,
            preview_rows=_result_preview(execution),
            preview_truncated=_result_preview_truncated(execution),
        )
        run_id = execution.run_id
        warnings = execution.warnings
        duration_ms = execution.duration_ms
        execution_source_columns = execution.source_column_ids
        status = execution.status.value

    execution_ref = ExecutionReference(
        run_id=run_id,
        plan_id=plan.plan_id,
        status=status,
        row_count=result_row_count,
        warnings=warnings,
        duration_ms=duration_ms,
        source_column_ids=execution_source_columns,
        result_columns=result_columns,
        result_sha256=result_sha256,
        error=execution_error,
    )
    code = render_plan_code(dataset, confirmed)
    evidence_key = "\0".join(
        (
            dataset.manifest.content_sha256,
            request.question_id,
            plan.plan_id,
            run_id or "no-run",
            result_sha256 or "no-result",
            verification.status.value,
        )
    )
    evidence_id = hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()
    return EvidenceRecord(
        evidence_id=evidence_id,
        version=EVIDENCE_VERSION,
        question_id=request.question_id,
        question=request.text,
        dataset_id=dataset.manifest.dataset_id,
        dataset_sha256=dataset.manifest.content_sha256,
        plan=plan,
        source_tables=source_tables,
        source_columns=source_columns,
        operations=operations,
        filters=tuple(
            operation
            for operation in operations
            if operation.kind is EvidenceOperationKind.FILTER
        ),
        transformations=tuple(
            operation
            for operation in operations
            if operation.kind
            in {
                EvidenceOperationKind.SORT,
                EvidenceOperationKind.LIMIT,
                EvidenceOperationKind.PROJECT,
            }
        ),
        calculations=tuple(
            operation
            for operation in operations
            if operation.kind is EvidenceOperationKind.GROUP_AGGREGATE
        ),
        source_row_count=len(source.rows),
        result=result_ref,
        execution=execution_ref,
        verification=_verification_reference(verification),
        generated_code=code,
        assumptions=plan.assumptions,
        limitations=tuple(
            dict.fromkeys(
                limitations
                + (
                    "Verification confirms computation against this snapshot; it does not "
                    "establish source-data truth, user intent, causality, or external business correctness.",
                )
            )
        ),
        component_versions=ComponentVersions(
            evidence=EVIDENCE_VERSION,
            plan=plan.version,
            executor=EXECUTOR_VERSION,
            verifier=verification.verifier_version,
            code_renderer=CODE_RENDERER_VERSION,
        ),
    )


def evidence_to_dict(evidence: EvidenceRecord) -> dict[str, object]:
    """Serialize the bounded evidence contract to JSON-compatible built-in values."""
    if type(evidence) is not EvidenceRecord:
        raise TypeError("Only an EvidenceRecord can be serialized.")
    converted = _json_value(evidence)
    if not isinstance(converted, dict):
        raise TypeError("Evidence serialization did not produce an object.")
    return converted


def serialize_evidence(evidence: EvidenceRecord) -> str:
    """Return canonical JSON suitable for stable storage or transport."""
    return json.dumps(
        evidence_to_dict(evidence),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _operation_references(plan: AnalysisPlan) -> tuple[OperationReference, ...]:
    operations = []
    for index, step in enumerate(plan.steps):
        if type(step) is Filter:
            operations.append(
                OperationReference(
                    index,
                    EvidenceOperationKind.FILTER,
                    (step.column_id,),
                    operator=step.operator.value,
                    value=step.value,
                )
            )
        elif type(step) is GroupAggregate:
            aggregation = step.aggregation
            column_ids = step.group_by + (
                (aggregation.column_id,)
                if aggregation.column_id is not None
                else ()
            )
            operations.append(
                OperationReference(
                    index,
                    EvidenceOperationKind.GROUP_AGGREGATE,
                    column_ids,
                    output_name=aggregation.output_name,
                    aggregation=aggregation.function.value,
                )
            )
        elif type(step) is Sort:
            column_ids = tuple(
                key.column_id for key in step.keys if key.column_id is not None
            )
            operations.append(
                OperationReference(
                    index,
                    EvidenceOperationKind.SORT,
                    column_ids,
                    ascending=tuple(key.ascending for key in step.keys),
                    output_names=tuple(
                        key.output_name
                        for key in step.keys
                        if key.output_name is not None
                    ),
                )
            )
        elif type(step) is Limit:
            operations.append(
                OperationReference(index, EvidenceOperationKind.LIMIT, limit=step.count)
            )
        elif type(step) is Project:
            operations.append(
                OperationReference(
                    index, EvidenceOperationKind.PROJECT, step.column_ids
                )
            )
        elif type(step) is SelectTable:
            continue
        else:
            raise ValueError("The validated plan contains an unsupported step.")
    return tuple(operations)


def _result_digest(execution: ExecutionResult) -> str:
    payload = (
        execution.result.columns,
        execution.result.rows,
        execution.result.row_lineage,
    )
    encoded = json.dumps(
        _json_value(payload),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_preview(execution: ExecutionResult) -> tuple[tuple[object, ...], ...]:
    preview = []
    for row in execution.result.rows[:MAX_EVIDENCE_PREVIEW_ROWS]:
        preview.append(
            tuple(
                _bounded_preview(value)[0]
                for value in row[:MAX_EVIDENCE_PREVIEW_COLUMNS]
            )
        )
    return tuple(preview)


def _result_preview_truncated(execution: ExecutionResult) -> bool:
    return (
        len(execution.result.rows) > MAX_EVIDENCE_PREVIEW_ROWS
        or len(execution.result.columns) > MAX_EVIDENCE_PREVIEW_COLUMNS
        or any(
            _bounded_preview(value)[1]
            for row in execution.result.rows[:MAX_EVIDENCE_PREVIEW_ROWS]
            for value in row[:MAX_EVIDENCE_PREVIEW_COLUMNS]
        )
    )


def _verification_reference(report: VerificationReport) -> VerificationReference:
    checks = []
    for check in report.checks:
        expected, expected_truncated = _bounded_preview(check.expected)
        observed, observed_truncated = _bounded_preview(check.observed)
        checks.append(
            VerificationCheckReference(
                name=check.name,
                passed=check.passed,
                expected_preview=expected,
                observed_preview=observed,
                tolerance=check.tolerance,
                detail=check.detail,
                preview_truncated=expected_truncated or observed_truncated,
            )
        )
    return VerificationReference(
        status=report.status,
        checks=tuple(checks),
        reasons=report.reasons,
        verifier_version=report.verifier_version,
    )


def _bounded_preview(value: object, depth: int = 0) -> tuple[object, bool]:
    if value is None or type(value) in {bool, int}:
        return value, False
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite verifier values cannot be included in evidence.")
        return value, False
    if isinstance(value, Enum):
        return value.value, False
    if type(value) in {datetime, date, time}:
        return f"{type(value).__name__}:{value.isoformat()}", False
    if isinstance(value, str):
        return (value[:253] + "..." if len(value) > 256 else value), len(value) > 256
    if isinstance(value, (tuple, list)):
        if depth >= 3:
            return f"<{type(value).__name__}:{len(value)} items>", True
        shown = value[:5]
        preview = []
        truncated = len(value) > len(shown)
        for item in shown:
            item_preview, item_truncated = _bounded_preview(item, depth + 1)
            preview.append(item_preview)
            truncated = truncated or item_truncated
        return tuple(preview), truncated
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        if depth >= 3:
            return f"<dict:{len(value)} items>", True
        items = sorted(value.items())[:5]
        preview = []
        truncated = len(value) > len(items)
        for key, item in items:
            item_preview, item_truncated = _bounded_preview(item, depth + 1)
            preview.append((key, item_preview))
            truncated = truncated or item_truncated
        return tuple(preview), truncated
    raise TypeError(
        f"Unsupported verifier value type for evidence preview: {type(value).__name__}."
    )


def _json_value(value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite values cannot be serialized as evidence.")
        return value
    if isinstance(value, Enum):
        return value.value
    if type(value) in {datetime, date, time}:
        return {"$type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Evidence object keys must be strings.")
        return {key: _json_value(value[key]) for key in sorted(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    raise TypeError(f"Unsupported evidence value type: {type(value).__name__}.")
