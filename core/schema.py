"""Stable identifiers, header checks, and conservative type inference."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

from models.schemas import CellValue, PhysicalType, SemanticHint

_IDENTIFIER_NAME = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:id|identifier|key|code)(?:$|[^A-Za-z0-9])|"
    r"(?:ID|Identifier|Key|Code)$"
)
_TRUE_FALSE = frozenset({"true", "false", "yes", "no", "y", "n"})
_SAMPLE_LIMIT = 10


def stable_id(prefix: str, value: str) -> str:
    """Return a deterministic opaque identifier from a namespace and value."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def validate_column_names(values: Sequence[object]) -> tuple[str, ...]:
    """Normalize blank and duplicate source headers to unique display names."""
    if not values:
        raise ValueError("table headers must contain at least one column")

    names: list[str] = []
    used: set[str] = set()
    for value in values:
        base = str(value) if value is not None else ""
        if not base.strip():
            base = "unnamed_column"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}__{suffix}"
            suffix += 1
        names.append(candidate)
        used.add(candidate)
    return tuple(names)


def infer_physical_type(values: Iterable[CellValue]) -> PhysicalType:
    """Infer the narrowest observed physical type without coercing cell data."""
    observed = {type(value) for value in values if value is not None}
    if not observed:
        return PhysicalType.EMPTY
    if observed == {bool}:
        return PhysicalType.BOOLEAN
    if observed <= {int, float}:
        return PhysicalType.FLOAT if float in observed else PhysicalType.INTEGER
    if observed == {str}:
        return PhysicalType.STRING
    if observed == {date}:
        return PhysicalType.DATE
    if observed == {datetime}:
        return PhysicalType.DATETIME
    if observed == {time}:
        return PhysicalType.TIME
    return PhysicalType.MIXED


def infer_semantic_hints(
    name: str, values: Sequence[CellValue], physical_type: PhysicalType
) -> tuple[SemanticHint, ...]:
    """Return only simple hints supported by the observed values and name."""
    non_null = tuple(value for value in values if value is not None)
    if not non_null:
        return ()

    hints: list[SemanticHint] = []
    strings = tuple(value for value in non_null if isinstance(value, str))
    normalized = tuple(value.strip().casefold() for value in strings)
    string_kinds = {_string_kind(value) for value in strings}

    if _IDENTIFIER_NAME.search(name):
        hints.append(SemanticHint.IDENTIFIER_LIKE)

    if physical_type in {PhysicalType.INTEGER, PhysicalType.FLOAT}:
        hints.append(SemanticHint.NUMERIC)
    elif strings and len(strings) == len(non_null) and all(
        _is_finite_decimal(value) for value in strings
    ):
        hints.append(SemanticHint.NUMERIC)

    if physical_type in {PhysicalType.DATE, PhysicalType.DATETIME, PhysicalType.TIME}:
        hints.append(SemanticHint.DATE_TIME)
    elif strings and len(strings) == len(non_null) and all(
        _is_iso_datetime(value) for value in strings
    ):
        hints.append(SemanticHint.DATE_TIME)

    if normalized and len(normalized) == len(non_null) and set(normalized) <= _TRUE_FALSE:
        hints.append(SemanticHint.BOOLEAN_LIKE)
    elif physical_type == PhysicalType.BOOLEAN:
        hints.append(SemanticHint.BOOLEAN_LIKE)

    if (
        strings
        and len(strings) == len(non_null)
        and len(string_kinds) > 1
        and SemanticHint.NUMERIC not in hints
        and SemanticHint.DATE_TIME not in hints
        and SemanticHint.BOOLEAN_LIKE not in hints
    ):
        hints.append(SemanticHint.AMBIGUOUS)

    distinct_count = len({_value_key(value) for value in non_null})
    categorical_threshold = min(50, max(2, len(non_null) // 5))
    if (
        physical_type == PhysicalType.STRING
        and len(string_kinds) == 1
        and string_kinds == {"text"}
        and distinct_count <= categorical_threshold
        and SemanticHint.IDENTIFIER_LIKE not in hints
        and SemanticHint.NUMERIC not in hints
        and SemanticHint.DATE_TIME not in hints
        and SemanticHint.BOOLEAN_LIKE not in hints
    ):
        hints.append(SemanticHint.CATEGORICAL)

    return tuple(hints)


def distinct_values(values: Iterable[CellValue]) -> tuple[CellValue, ...]:
    """Return non-null distinct values in their first-seen order."""
    result: list[CellValue] = []
    seen: set[tuple[str, object]] = set()
    for value in values:
        if value is None:
            continue
        key = _value_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)


def _is_finite_decimal(value: str) -> bool:
    try:
        return Decimal(value.strip()).is_finite()
    except InvalidOperation:
        return False


def _string_kind(value: str) -> str:
    if _is_finite_decimal(value):
        return "numeric"
    if _is_iso_datetime(value):
        return "date/time"
    if value.strip().casefold() in _TRUE_FALSE:
        return "boolean"
    return "text"


def _is_iso_datetime(value: str) -> bool:
    candidate = value.strip()
    try:
        date.fromisoformat(candidate)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        pass
    try:
        time.fromisoformat(candidate)
        return True
    except ValueError:
        return False


def _value_key(value: CellValue) -> tuple[str, object]:
    return (type(value).__name__, value)


def sample_values(values: Iterable[CellValue], limit: int) -> tuple[CellValue, ...]:
    """Return a deterministic, distinct, non-null sample with a hard cap."""
    if not 0 <= limit <= _SAMPLE_LIMIT:
        raise ValueError(f"sample limit must be between 0 and {_SAMPLE_LIMIT}")
    return distinct_values(values)[:limit]
