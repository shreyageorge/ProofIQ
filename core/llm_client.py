"""Provider-neutral proposal boundary and strict parsing for LLM plan output."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Protocol, runtime_checkable

from core.profiler import profile_dataset
from core.schema import stable_id
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    IngestedDataset,
    Limit,
    OutputSpecification,
    Project,
    QuestionIntent,
    RequestedCapability,
    SelectTable,
    Sort,
    SortKey,
)

MAX_QUESTION_LENGTH = 4_000
MAX_CATALOG_CONTEXT_BYTES = 64_000
MAX_PROPOSAL_BYTES = 16_000
MAX_PROPOSAL_STEPS = 11
MAX_PROPOSAL_FIELDS = 100
MAX_PROPOSAL_ASSUMPTIONS = 20


class ProposalErrorCode(str, Enum):
    MALFORMED_OUTPUT = "malformed_output"
    INVALID_PROPOSAL = "invalid_proposal"
    UNSUPPORTED_OPERATION = "unsupported_operation"


class PlanProposalError(ValueError):
    """A safe, non-echoing error raised for malformed or unsupported proposals."""

    def __init__(self, code: ProposalErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class LLMClientError(RuntimeError):
    """Base exception for provider availability and request errors."""


class LLMTimeoutError(LLMClientError):
    """The provider did not return before its configured timeout."""


class LLMUnavailableError(LLMClientError):
    """No configured provider or mock response is available."""


class LLMRequestError(LLMClientError):
    """The provider rejected or could not process the proposal request."""


class CatalogContextError(ValueError):
    """The safe metadata catalog cannot fit within its configured prompt budget."""


@dataclass(frozen=True, slots=True)
class CatalogColumn:
    column_id: str
    name: str
    physical_type: str
    semantic_hints: tuple[str, ...]
    null_count: int
    distinct_count: int


@dataclass(frozen=True, slots=True)
class CatalogTable:
    table_id: str
    display_name: str
    row_count: int
    columns: tuple[CatalogColumn, ...]


@dataclass(frozen=True, slots=True)
class CatalogContext:
    """Bounded schema-only context; it intentionally contains no cell samples."""

    dataset_id: str
    dataset_sha256: str
    tables: tuple[CatalogTable, ...]
    serialized_bytes: int


@dataclass(frozen=True, slots=True)
class ParsedPlanProposal:
    plan: AnalysisPlan
    intent: QuestionIntent


@runtime_checkable
class LLMClient(Protocol):
    """Small provider seam. Implementations return structured data, never code."""

    def generate_plan(
        self,
        question: str,
        catalog_context: CatalogContext,
        *,
        timeout_seconds: float,
    ) -> object:
        """Return an untrusted JSON object/string proposing an analysis plan."""


class MockLLMClient:
    """Deterministic offline client keyed by exact normalized question text."""

    def __init__(
        self,
        responses: dict[str, object] | None = None,
        *,
        error: LLMClientError | None = None,
    ) -> None:
        self._responses = {
            question.strip().casefold(): response
            for question, response in (responses or {}).items()
        }
        self._error = error
        self.call_count = 0
        self.last_context: CatalogContext | None = None

    def generate_plan(
        self,
        question: str,
        catalog_context: CatalogContext,
        *,
        timeout_seconds: float,
    ) -> object:
        if (
            type(question) is not str
            or type(catalog_context) is not CatalogContext
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise LLMRequestError("The proposal request is invalid.")
        self.call_count += 1
        self.last_context = catalog_context
        if self._error is not None:
            raise self._error
        try:
            return self._responses[question.strip().casefold()]
        except KeyError:
            raise LLMUnavailableError("No mock proposal is configured.") from None


def build_catalog_context(
    dataset: IngestedDataset,
    *,
    max_context_bytes: int = MAX_CATALOG_CONTEXT_BYTES,
) -> CatalogContext:
    """Build metadata-only context and reject rather than silently truncate it."""
    if type(dataset) is not IngestedDataset:
        raise CatalogContextError("A valid ingested dataset is required.")
    if type(max_context_bytes) is not int or max_context_bytes < 1:
        raise CatalogContextError("max_context_bytes must be a positive integer.")
    profile = profile_dataset(dataset)
    tables = tuple(
        CatalogTable(
            table_id=table.table_id,
            display_name=ref.display_name,
            row_count=table.row_count,
            columns=tuple(
                CatalogColumn(
                    column_id=column.column_id,
                    name=column.name,
                    physical_type=column.physical_type.value,
                    semantic_hints=tuple(hint.value for hint in column.semantic_hints),
                    null_count=column.null_count,
                    distinct_count=column.distinct_count,
                )
                for column in table.columns
            ),
        )
        for table, ref in zip(profile.tables, dataset.manifest.tables, strict=True)
    )
    encoded = json.dumps(
        {
            "dataset_id": dataset.manifest.dataset_id,
            "dataset_sha256": dataset.manifest.content_sha256,
            "tables": [
                {
                    "table_id": table.table_id,
                    "display_name": table.display_name,
                    "row_count": table.row_count,
                    "columns": [
                        {
                            "column_id": column.column_id,
                            "name": column.name,
                            "physical_type": column.physical_type,
                            "semantic_hints": column.semantic_hints,
                            "null_count": column.null_count,
                            "distinct_count": column.distinct_count,
                        }
                        for column in table.columns
                    ],
                }
                for table in tables
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_context_bytes:
        raise CatalogContextError(
            "The dataset catalog exceeds the bounded proposal context."
        )
    return CatalogContext(
        dataset_id=dataset.manifest.dataset_id,
        dataset_sha256=dataset.manifest.content_sha256,
        tables=tables,
        serialized_bytes=len(encoded),
    )


def parse_plan_proposal(
    raw: object,
    *,
    question_id: str,
    dataset: IngestedDataset,
    context: CatalogContext,
) -> ParsedPlanProposal:
    """Parse strict JSON-like proposal data into existing typed contracts."""
    if (
        type(question_id) is not str
        or not question_id.strip()
        or type(dataset) is not IngestedDataset
        or type(context) is not CatalogContext
        or context.dataset_id != dataset.manifest.dataset_id
        or context.dataset_sha256 != dataset.manifest.content_sha256
    ):
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Proposal context does not match the active dataset and question.",
        )
    payload = _decode_proposal(raw)
    _check_json_depth(payload)
    _require_object_keys(
        payload,
        required={"table_id", "required_fields", "steps"},
        optional={"version", "optional_fields", "assumptions", "output_columns"},
    )
    if payload.get("version", 1) != 1 or type(payload.get("version", 1)) is not int:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Only proposal version 1 is supported.",
        )
    table_id = _required_string(payload, "table_id")
    required_fields = _string_list(payload, "required_fields", max_items=MAX_PROPOSAL_FIELDS)
    optional_fields = _string_list(
        payload, "optional_fields", required=False, max_items=MAX_PROPOSAL_FIELDS
    )
    assumptions = _string_list(
        payload, "assumptions", required=False, max_items=MAX_PROPOSAL_ASSUMPTIONS
    )
    output_columns = _string_list(
        payload, "output_columns", required=False, max_items=MAX_PROPOSAL_FIELDS
    )
    if set(required_fields).intersection(optional_fields):
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Required and optional fields must not overlap.",
        )

    raw_steps = payload["steps"]
    if type(raw_steps) is not list or len(raw_steps) > MAX_PROPOSAL_STEPS:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "The proposal steps must be a bounded JSON array.",
        )
    steps = tuple(_parse_step(step) for step in raw_steps)
    typed_steps = (SelectTable(table_id), *steps)
    output_spec = OutputSpecification(
        tuple(output_columns) if "output_columns" in payload else None
    )
    plan_seed = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    plan_id = stable_id(
        "plan",
        f"{dataset.manifest.content_sha256}\0{question_id}\0{plan_seed}",
    )
    plan = AnalysisPlan(
        plan_id=plan_id,
        question_id=question_id,
        dataset_id=dataset.manifest.dataset_id,
        steps=typed_steps,
        output_spec=output_spec,
        assumptions=tuple(assumptions),
    )
    table = next((entry for entry in context.tables if entry.table_id == table_id), None)
    intent = QuestionIntent(
        question_id=question_id,
        dataset_id=dataset.manifest.dataset_id,
        required_fields=tuple(required_fields),
        optional_fields=tuple(optional_fields),
        table_name=table.display_name if table is not None else table_id,
        capabilities=_capabilities(steps),
    )
    return ParsedPlanProposal(plan=plan, intent=intent)


def _decode_proposal(raw: object) -> dict[str, object]:
    if type(raw) is str:
        encoded = raw.encode("utf-8")
    elif type(raw) is dict:
        try:
            encoded = json.dumps(raw, ensure_ascii=True, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise PlanProposalError(
                ProposalErrorCode.MALFORMED_OUTPUT,
                "The provider response is not valid JSON data.",
            ) from None
    else:
        raise PlanProposalError(
            ProposalErrorCode.MALFORMED_OUTPUT,
            "The provider response must be a JSON object or JSON string.",
        )
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "The provider response exceeds the proposal size limit.",
        )
    try:
        decoded = json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PlanProposalError(
            ProposalErrorCode.MALFORMED_OUTPUT,
            "The provider response is not valid JSON.",
        ) from None
    if type(decoded) is not dict or any(type(key) is not str for key in decoded):
        raise PlanProposalError(
            ProposalErrorCode.MALFORMED_OUTPUT,
            "The provider response must be a JSON object with string keys.",
        )
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are not supported.")
        result[key] = value
    return result


def _reject_constant(_constant: str) -> None:
    raise ValueError("Non-standard JSON numeric constants are not supported.")


def _check_json_depth(value: object, depth: int = 0) -> None:
    if depth > 8:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "The provider response is nested too deeply.",
        )
    if type(value) is dict:
        for key, item in value.items():
            if len(key) > 512:
                raise PlanProposalError(
                    ProposalErrorCode.INVALID_PROPOSAL,
                    "A proposal field name is too long.",
                )
            _check_json_depth(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _check_json_depth(item, depth + 1)
    elif type(value) is str and len(value) > MAX_PROPOSAL_BYTES:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal string exceeds the size limit.",
        )


def _parse_step(raw: object):
    if type(raw) is not dict or type(raw.get("type")) is not str:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Each proposal step must be a typed JSON object.",
        )
    step_type = raw["type"].casefold()
    schemas = {
        "filter": ({"type", "column_id", "operator", "value"}, set()),
        "group_aggregate": ({"type", "group_by", "aggregation"}, set()),
        "sort": ({"type", "keys"}, set()),
        "limit": ({"type", "count"}, set()),
        "project": ({"type", "column_ids"}, set()),
    }
    if step_type not in schemas:
        raise PlanProposalError(
            ProposalErrorCode.UNSUPPORTED_OPERATION,
            "The proposal contains an unsupported operation.",
        )
    required, optional = schemas[step_type]
    _require_object_keys(raw, required=required, optional=optional)
    if step_type == "filter":
        column_id = _required_string(raw, "column_id")
        operator = _enum_value(FilterOperator, raw.get("operator"))
        value = raw.get("value")
        if operator is FilterOperator.IN:
            if type(value) is not list or not value:
                raise PlanProposalError(
                    ProposalErrorCode.INVALID_PROPOSAL,
                    "IN filters require a non-empty array of literal values.",
                )
            value = tuple(_cell_literal(item) for item in value)
        else:
            value = _cell_literal(value)
        return Filter(column_id, operator, value)
    if step_type == "group_aggregate":
        raw_groups = raw.get("group_by")
        if type(raw_groups) is not list or len(raw_groups) > MAX_PROPOSAL_FIELDS:
            raise PlanProposalError(
                ProposalErrorCode.INVALID_PROPOSAL,
                "group_by must be a bounded array of column IDs.",
            )
        group_by = tuple(_string_value(item) for item in raw_groups)
        aggregation_raw = raw.get("aggregation")
        _require_object_keys(
            aggregation_raw,
            required={"function"},
            optional={"column_id", "output_name"},
        )
        function = _enum_value(AggregationFunction, aggregation_raw.get("function"))
        column_id = aggregation_raw.get("column_id")
        if column_id is not None:
            column_id = _string_value(column_id)
        output_name = aggregation_raw.get("output_name", "value")
        output_name = _string_value(output_name)
        return GroupAggregate(
            group_by,
            Aggregation(function, column_id, output_name),
        )
    if step_type == "sort":
        raw_keys = raw.get("keys")
        if type(raw_keys) is not list or not raw_keys or len(raw_keys) > 500:
            raise PlanProposalError(
                ProposalErrorCode.INVALID_PROPOSAL,
                "Sort requires a bounded, non-empty array of keys.",
            )
        keys = []
        for raw_key in raw_keys:
            _require_object_keys(
                raw_key,
                required={"ascending"},
                optional={"column_id", "output_name"},
            )
            column_id = raw_key.get("column_id")
            output_name = raw_key.get("output_name")
            if (column_id is None) == (output_name is None):
                raise PlanProposalError(
                    ProposalErrorCode.INVALID_PROPOSAL,
                    "Each sort key must identify one source or output column.",
                )
            if column_id is not None:
                column_id = _string_value(column_id)
            if output_name is not None:
                output_name = _string_value(output_name)
            ascending = raw_key.get("ascending")
            if type(ascending) is not bool:
                raise PlanProposalError(
                    ProposalErrorCode.INVALID_PROPOSAL,
                    "Sort direction must be a boolean.",
                )
            keys.append(SortKey(ascending, column_id, output_name))
        return Sort(tuple(keys))
    if step_type == "limit":
        count = raw.get("count")
        if type(count) is not int:
            raise PlanProposalError(
                ProposalErrorCode.INVALID_PROPOSAL,
                "Limit must be an integer.",
            )
        return Limit(count)
    raw_columns = raw.get("column_ids")
    if type(raw_columns) is not list or not raw_columns:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Projection requires a non-empty array of column IDs.",
        )
    return Project(tuple(_string_value(column_id) for column_id in raw_columns))


def _capabilities(steps: tuple[object, ...]) -> tuple[RequestedCapability, ...]:
    capabilities: list[RequestedCapability] = []
    for step in steps:
        if type(step) is Filter:
            capabilities.append(RequestedCapability.FILTER)
        elif type(step) is GroupAggregate:
            if step.group_by:
                capabilities.append(RequestedCapability.GROUP)
            capabilities.append(RequestedCapability.AGGREGATE)
        elif type(step) is Sort:
            capabilities.append(RequestedCapability.SORT)
        elif type(step) is Limit:
            capabilities.append(RequestedCapability.LIMIT)
        elif type(step) is Project:
            capabilities.append(RequestedCapability.PROJECT)
    return tuple(dict.fromkeys(capabilities))


def _require_object_keys(
    value: object, *, required: set[str], optional: set[str]
) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal object is missing or malformed.",
        )
    keys = set(value)
    if not required.issubset(keys):
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal object is missing required fields.",
        )
    if keys.difference(required | optional):
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal object contains unsupported fields.",
        )


def _required_string(value: dict[str, object], key: str) -> str:
    if key not in value:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A required proposal field is missing.",
        )
    return _string_value(value[key])


def _string_value(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "Proposal identifiers and labels must be short non-empty strings.",
        )
    return value


def _string_list(
    value: dict[str, object],
    key: str,
    *,
    required: bool = True,
    max_items: int,
) -> list[str]:
    if key not in value:
        if required:
            raise PlanProposalError(
                ProposalErrorCode.INVALID_PROPOSAL,
                "A required proposal field is missing.",
            )
        return []
    items = value[key]
    if type(items) is not list or len(items) > max_items:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal field must be a bounded array.",
        )
    return [_string_value(item) for item in items]


def _enum_value(enum_type: type[Enum], raw: object):
    if type(raw) is not str:
        raise PlanProposalError(
            ProposalErrorCode.INVALID_PROPOSAL,
            "A proposal operation enum is invalid.",
        )
    normalized = raw.casefold()
    for member in enum_type:
        if member.name.casefold() == normalized or member.value.casefold() == normalized:
            return member
    raise PlanProposalError(
        ProposalErrorCode.UNSUPPORTED_OPERATION,
        "The proposal uses an unsupported operator or aggregation.",
    )


def _cell_literal(value: object):
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if (
        type(value) is dict
        and set(value) == {"type", "value"}
        and type(value["type"]) is str
        and type(value["value"]) is str
    ):
        constructors = {
            "date": date.fromisoformat,
            "datetime": datetime.fromisoformat,
            "time": time.fromisoformat,
        }
        constructor = constructors.get(value["type"].casefold())
        if constructor is not None:
            try:
                return constructor(value["value"])
            except ValueError:
                raise PlanProposalError(
                    ProposalErrorCode.INVALID_PROPOSAL,
                    "A typed date/time filter literal is invalid.",
                ) from None
    raise PlanProposalError(
        ProposalErrorCode.INVALID_PROPOSAL,
        "Filter values must be finite JSON scalars or typed date/time literals.",
    )
