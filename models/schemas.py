"""Immutable contracts shared by ingestion and deterministic profiling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum

CellValue = str | int | float | bool | date | datetime | time | None


class DatasetFormat(str, Enum):
    """Supported uploaded file formats."""

    CSV = "csv"
    XLSX = "xlsx"


class PhysicalType(str, Enum):
    """Observed cell representation, not a claim about business meaning."""

    EMPTY = "empty"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FLOAT = "float"
    STRING = "string"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    MIXED = "mixed"


class SemanticHint(str, Enum):
    """Conservative, deterministic hints inferred from column contents."""

    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    DATE_TIME = "date/time"
    IDENTIFIER_LIKE = "identifier-like"
    BOOLEAN_LIKE = "boolean-like"
    AMBIGUOUS = "ambiguous"


class AnswerabilityStatus(str, Enum):
    ANSWERABLE = "answerable"
    PARTIALLY_ANSWERABLE = "partially_answerable"
    CANNOT_DETERMINE = "cannot_determine"
    NEEDS_CLARIFICATION = "needs_clarification"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"


class ExecutionStatus(str, Enum):
    SUCCESS = "success"


class FilterOperator(str, Enum):
    EQ = "eq"
    IN = "in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IS_NULL = "is_null"


class AggregationFunction(str, Enum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"


class RequestedCapability(str, Enum):
    FILTER = "filter"
    GROUP = "group"
    AGGREGATE = "aggregate"
    SORT = "sort"
    LIMIT = "limit"
    PROJECT = "project"
    JOIN = "join"
    DERIVE = "derive"
    RANK = "rank"


class IngestionErrorCode(str, Enum):
    """Stable error categories for invalid or unsupported uploads."""

    INVALID_FILE = "invalid_file"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_TOO_LARGE = "file_too_large"
    LIMIT_EXCEEDED = "limit_exceeded"
    INVALID_SCHEMA = "invalid_schema"
    SHEET_NOT_FOUND = "sheet_not_found"
    EMPTY_DATASET = "empty_dataset"


class IngestionError(ValueError):
    """A safe, user-displayable error raised for rejected upload data."""

    def __init__(self, code: IngestionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    """Resource bounds applied before and during upload parsing."""

    max_file_size_bytes: int = 25_000_000
    max_rows: int = 100_000
    max_columns: int = 500
    max_xlsx_sheets: int = 50
    max_cells: int = 2_000_000
    max_xlsx_uncompressed_bytes: int = 100_000_000

    def __post_init__(self) -> None:
        """Reject non-positive limits at configuration time."""
        for field_name, value in (
            ("max_file_size_bytes", self.max_file_size_bytes),
            ("max_rows", self.max_rows),
            ("max_columns", self.max_columns),
            ("max_xlsx_sheets", self.max_xlsx_sheets),
            ("max_cells", self.max_cells),
            ("max_xlsx_uncompressed_bytes", self.max_xlsx_uncompressed_bytes),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class ColumnLineage:
    """Source location for a column in an uploaded file."""

    source_file_id: str
    source_sheet: str | None
    source_column_index: int


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Deterministic statistics and bounded examples for one source column."""

    column_id: str
    name: str
    physical_type: PhysicalType
    semantic_hints: tuple[SemanticHint, ...]
    null_count: int
    distinct_count: int
    sample_values: tuple[CellValue, ...]
    source_lineage: ColumnLineage


@dataclass(frozen=True, slots=True)
class TableRef:
    """Stable reference to a logical table within an uploaded dataset."""

    table_id: str
    display_name: str
    row_count: int
    column_count: int
    source_file_id: str
    source_sheet: str | None


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    """Immutable table values and stable source-row identifiers."""

    table_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[CellValue, ...], ...]
    row_ids: tuple[str, ...]
    source_row_numbers: tuple[int, ...]

    def __post_init__(self) -> None:
        """Enforce alignment between rows, columns, and source identities."""
        if len(self.rows) != len(self.row_ids) or len(self.rows) != len(
            self.source_row_numbers
        ):
            raise ValueError("table rows and row metadata must have equal lengths")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("each table row must match the column count")
        if len(set(self.row_ids)) != len(self.row_ids):
            raise ValueError("row identifiers must be unique within a table")


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """Content-addressed metadata for one upload and its logical tables."""

    dataset_id: str
    content_sha256: str
    original_name: str
    file_format: DatasetFormat
    tables: tuple[TableRef, ...]
    limits_applied: IngestionLimits


@dataclass(frozen=True, slots=True)
class IngestedDataset:
    """Manifest and immutable in-memory table snapshots."""

    manifest: DatasetManifest
    tables: tuple[TableSnapshot, ...]

    def __post_init__(self) -> None:
        """Ensure every manifest table resolves to exactly one snapshot."""
        manifest_ids = tuple(table.table_id for table in self.manifest.tables)
        snapshot_ids = tuple(table.table_id for table in self.tables)
        if manifest_ids != snapshot_ids:
            raise ValueError("manifest tables and table snapshots must align")


@dataclass(frozen=True, slots=True)
class TableProfile:
    """Counts and column profiles for a logical table."""

    table_id: str
    row_count: int
    column_count: int
    duplicate_row_count: int
    columns: tuple[ColumnProfile, ...]


@dataclass(frozen=True, slots=True)
class DatasetProfile:
    """Deterministic catalog for all tables in an ingested dataset."""

    manifest: DatasetManifest
    tables: tuple[TableProfile, ...]

    def __post_init__(self) -> None:
        """Ensure profile order and table identity match the manifest."""
        manifest_ids = tuple(table.table_id for table in self.manifest.tables)
        profile_ids = tuple(table.table_id for table in self.tables)
        if manifest_ids != profile_ids:
            raise ValueError("manifest tables and table profiles must align")


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    """A future analysis request bound to one ingested dataset."""

    question_id: str
    text: str
    dataset_id: str
    locale: str | None = None

    def __post_init__(self) -> None:
        """Reject incomplete request identity or question text."""
        if not self.question_id.strip():
            raise ValueError("question_id must not be empty")
        if not self.text.strip():
            raise ValueError("question text must not be empty")
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must not be empty")


@dataclass(frozen=True, slots=True)
class QuestionIntent:
    """Deterministic, structured field requirements for the answerability gate."""

    question_id: str
    dataset_id: str
    required_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    table_name: str | None = None
    capabilities: tuple[RequestedCapability, ...] = ()


@dataclass(frozen=True, slots=True)
class Answerability:
    status: AnswerabilityStatus
    reasons: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    resolved_table_id: str | None = None
    resolved_column_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Filter:
    column_id: str
    operator: FilterOperator
    value: CellValue | tuple[CellValue, ...] | None = None


@dataclass(frozen=True, slots=True)
class SelectTable:
    table_id: str


@dataclass(frozen=True, slots=True)
class Aggregation:
    function: AggregationFunction
    column_id: str | None = None
    output_name: str = "value"


@dataclass(frozen=True, slots=True)
class GroupAggregate:
    group_by: tuple[str, ...]
    aggregation: Aggregation


@dataclass(frozen=True, slots=True)
class SortKey:
    ascending: bool = True
    column_id: str | None = None
    output_name: str | None = None


@dataclass(frozen=True, slots=True)
class Sort:
    keys: tuple[SortKey, ...]


@dataclass(frozen=True, slots=True)
class Limit:
    count: int


@dataclass(frozen=True, slots=True)
class Project:
    column_ids: tuple[str, ...]


PlanStep = SelectTable | Filter | GroupAggregate | Sort | Limit | Project


@dataclass(frozen=True, slots=True)
class OutputSpecification:
    """Optional expected result column names, in order."""

    columns: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    plan_id: str
    question_id: str
    dataset_id: str
    steps: tuple[PlanStep, ...]
    output_spec: OutputSpecification = OutputSpecification()
    assumptions: tuple[str, ...] = ()
    version: int = 1


@dataclass(frozen=True, slots=True)
class PlanValidation:
    accepted: bool
    validated_plan: AnalysisPlan | None
    issues: tuple[str, ...] = ()
    answerability_status: AnswerabilityStatus = AnswerabilityStatus.ANSWERABLE


@dataclass(frozen=True, slots=True)
class ResultTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[CellValue, ...], ...]
    row_lineage: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("each result row must match the result columns")
        if len(self.rows) != len(self.row_lineage):
            raise ValueError("result rows and row lineage must have equal lengths")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    plan_id: str
    status: ExecutionStatus
    result: ResultTable
    row_count: int
    warnings: tuple[str, ...]
    duration_ms: float
    source_column_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    passed: bool
    expected: object
    observed: object
    tolerance: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class VerificationReport:
    status: VerificationStatus
    checks: tuple[VerificationCheck, ...]
    reasons: tuple[str, ...]
    verifier_version: str
