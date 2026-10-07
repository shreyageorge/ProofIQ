"""Deterministic resolution of structured question requirements against a catalog."""

from __future__ import annotations

from models.schemas import (
    Answerability,
    AnswerabilityStatus,
    IngestedDataset,
    QuestionIntent,
    RequestedCapability,
)
from core.profiler import profile_dataset

_SUPPORTED_CAPABILITIES = {
    RequestedCapability.FILTER,
    RequestedCapability.GROUP,
    RequestedCapability.AGGREGATE,
    RequestedCapability.SORT,
    RequestedCapability.LIMIT,
    RequestedCapability.PROJECT,
}


def assess_answerability(
    dataset: IngestedDataset, intent: QuestionIntent
) -> Answerability:
    """Resolve exact field names without guessing or authorizing execution."""
    if type(dataset) is not IngestedDataset or type(intent) is not QuestionIntent:
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "A dataset and structured question intent are required.",
        )
    if intent.dataset_id != dataset.manifest.dataset_id:
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "The question intent refers to a different dataset.",
        )
    if not isinstance(intent.question_id, str) or not intent.question_id.strip():
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "A non-empty question ID is required.",
        )
    if not isinstance(intent.capabilities, tuple) or any(
        type(capability) is not RequestedCapability
        for capability in intent.capabilities
    ):
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "Question capabilities must be a tuple of supported typed values.",
        )
    unsupported = tuple(
        capability.value
        for capability in intent.capabilities
        if capability not in _SUPPORTED_CAPABILITIES
    )
    if unsupported:
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            f"Unsupported requested operation(s): {', '.join(unsupported)}.",
            limitations=("Joins, derived formulas, and ranking are not supported.",),
        )
    if not _valid_field_names(intent.required_fields) or not _valid_field_names(
        intent.optional_fields
    ):
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "Required and optional fields must be tuples of non-empty names.",
        )
    if (
        len(set(intent.required_fields)) != len(intent.required_fields)
        or len(set(intent.optional_fields)) != len(intent.optional_fields)
        or set(intent.required_fields).intersection(intent.optional_fields)
    ):
        return _assessment(
            AnswerabilityStatus.CANNOT_DETERMINE,
            "Required and optional fields must not contain duplicates or overlap.",
        )
    if (
        not intent.required_fields
        and not intent.optional_fields
        and not intent.capabilities
    ):
        return _assessment(
            AnswerabilityStatus.NEEDS_CLARIFICATION,
            "No fields or supported operation were specified for the question.",
        )

    profiles = {table.table_id: table for table in profile_dataset(dataset).tables}
    refs = {table.table_id: table for table in dataset.manifest.tables}
    candidate_ids = tuple(refs)
    if intent.table_name is not None:
        if not isinstance(intent.table_name, str) or not intent.table_name.strip():
            return _assessment(
                AnswerabilityStatus.CANNOT_DETERMINE,
                "The requested table name is invalid.",
            )
        candidate_ids = tuple(
            table_id
            for table_id, ref in refs.items()
            if ref.display_name.casefold() == intent.table_name.casefold()
        )
        if not candidate_ids:
            return _assessment(
                AnswerabilityStatus.CANNOT_DETERMINE,
                f"Table {intent.table_name!r} is not present in the dataset.",
            )

    matches: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    ambiguous: list[str] = []
    missing_by_table: list[tuple[str, ...]] = []
    for table_id in candidate_ids:
        columns = profiles[table_id].columns
        by_name: dict[str, list[str]] = {}
        for column in columns:
            by_name.setdefault(column.name.casefold(), []).append(column.column_id)
        resolved: list[str] = []
        missing: list[str] = []
        ambiguous_fields: list[str] = []
        for field_name in intent.required_fields:
            candidates = by_name.get(field_name.casefold(), [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous_fields.append(field_name)
            else:
                missing.append(field_name)
        optional_missing: list[str] = []
        for field_name in intent.optional_fields:
            candidates = by_name.get(field_name.casefold(), [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous_fields.append(field_name)
            else:
                optional_missing.append(field_name)
        if ambiguous_fields:
            ambiguous.extend(ambiguous_fields)
        elif not missing:
            matches.append((table_id, tuple(resolved), tuple(optional_missing)))
        else:
            missing_by_table.append(tuple(missing))

    if ambiguous:
        fields = tuple(dict.fromkeys(ambiguous))
        return Answerability(
            status=AnswerabilityStatus.NEEDS_CLARIFICATION,
            reasons=("A requested field matches multiple columns; choose one explicitly.",),
            missing_fields=fields,
            limitations=("Column resolution uses exact case-insensitive names only.",),
        )
    if len(matches) > 1:
        return Answerability(
            status=AnswerabilityStatus.NEEDS_CLARIFICATION,
            reasons=("Multiple tables satisfy the requested fields; select a table.",),
            limitations=("The system does not infer table intent.",),
        )
    if not matches:
        missing = (
            tuple(dict.fromkeys(name for group in missing_by_table for name in group))
            if missing_by_table
            else intent.required_fields
        )
        return Answerability(
            status=AnswerabilityStatus.CANNOT_DETERMINE,
            reasons=("One or more required fields are unavailable.",),
            missing_fields=missing,
            limitations=("No similar or derived columns are substituted.",),
        )

    table_id, resolved_ids, optional_missing = matches[0]
    if optional_missing:
        return Answerability(
            status=AnswerabilityStatus.PARTIALLY_ANSWERABLE,
            reasons=("Required fields are available, but optional fields are missing.",),
            missing_fields=optional_missing,
            assumptions=("The result is limited to the required fields.",),
            limitations=("Optional requested fields were not included.",),
            resolved_table_id=table_id,
            resolved_column_ids=resolved_ids,
        )
    return Answerability(
        status=AnswerabilityStatus.ANSWERABLE,
        reasons=("All required fields resolve unambiguously in one table.",),
        assumptions=("Field names are matched exactly, ignoring letter case.",),
        limitations=("This assessment does not authorize plan execution.",),
        resolved_table_id=table_id,
        resolved_column_ids=resolved_ids,
    )


def _valid_field_names(values: object) -> bool:
    return isinstance(values, tuple) and all(
        isinstance(value, str) and bool(value.strip()) for value in values
    )


def _assessment(status: AnswerabilityStatus, reason: str, **kwargs: object) -> Answerability:
    return Answerability(status=status, reasons=(reason,), **kwargs)
