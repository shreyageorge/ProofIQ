"""Offline tests for the structured proposal boundary and mock provider."""

import json
from datetime import datetime

import pytest

from core.ingestion import ingest_upload
from core.llm_client import (
    MAX_PROPOSAL_BYTES,
    LLMClient,
    LLMRequestError,
    LLMTimeoutError,
    MockLLMClient,
    PlanProposalError,
    ProposalErrorCode,
    build_catalog_context,
    parse_plan_proposal,
)


def _proposal(dataset, ids, **overrides):
    proposal = {
        "version": 1,
        "table_id": dataset.tables[0].table_id,
        "required_fields": ["amount"],
        "steps": [
            {
                "type": "group_aggregate",
                "group_by": [],
                "aggregation": {
                    "function": "SUM",
                    "column_id": ids["amount"],
                    "output_name": "total",
                },
            }
        ],
    }
    proposal.update(overrides)
    return proposal


def test_mock_provider_returns_structured_proposal_without_credentials(
    analysis_dataset, analysis_column_ids
) -> None:
    question = "What is the total amount?"
    proposal = _proposal(analysis_dataset, analysis_column_ids)
    client = MockLLMClient({question: proposal})
    context = build_catalog_context(analysis_dataset)

    raw = client.generate_plan(question, context, timeout_seconds=2)
    parsed = parse_plan_proposal(
        raw,
        question_id="question-1",
        dataset=analysis_dataset,
        context=context,
    )

    assert parsed.plan.steps[0].table_id == analysis_dataset.tables[0].table_id
    assert parsed.intent.required_fields == ("amount",)
    assert client.call_count == 1
    assert client.last_context == context
    assert isinstance(client, LLMClient)


def test_provider_protocol_is_independent_of_the_mock(
    analysis_dataset, analysis_column_ids
) -> None:
    class LocalProvider:
        def generate_plan(self, question, catalog_context, *, timeout_seconds):
            assert question == "What is the total amount?"
            assert catalog_context.dataset_id == analysis_dataset.manifest.dataset_id
            assert timeout_seconds > 0
            return json.dumps(_proposal(analysis_dataset, analysis_column_ids))

    provider: LLMClient = LocalProvider()
    context = build_catalog_context(analysis_dataset)
    assert isinstance(
        provider.generate_plan("What is the total amount?", context, timeout_seconds=1),
        str,
    )


def test_mock_provider_errors_and_context_limits_are_explicit(analysis_dataset) -> None:
    context = build_catalog_context(analysis_dataset)
    timeout_client = MockLLMClient(error=LLMTimeoutError("timeout"))
    with pytest.raises(LLMTimeoutError):
        timeout_client.generate_plan("question", context, timeout_seconds=1)

    with pytest.raises(ValueError, match="bounded proposal context"):
        build_catalog_context(analysis_dataset, max_context_bytes=1)
    with pytest.raises(LLMRequestError, match="proposal request"):
        MockLLMClient().generate_plan("question", context, timeout_seconds=0)


def test_catalog_context_contains_bounded_schema_metadata_only(
    analysis_dataset,
) -> None:
    context = build_catalog_context(analysis_dataset)

    assert context.serialized_bytes > 0
    assert context.dataset_sha256 == analysis_dataset.manifest.content_sha256
    assert not hasattr(context.tables[0].columns[0], "sample_values")
    assert "North" not in repr(context)
    assert "100" not in repr(context)


def test_parser_rejects_malformed_missing_and_extra_fields(
    analysis_dataset, analysis_column_ids
) -> None:
    context = build_catalog_context(analysis_dataset)
    kwargs = {
        "question_id": "question-1",
        "dataset": analysis_dataset,
        "context": context,
    }

    for malformed in (
        "{",
        [],
        None,
        '{"steps": NaN}',
        '{"table_id":"first","table_id":"second"}',
    ):
        with pytest.raises(PlanProposalError) as error:
            parse_plan_proposal(malformed, **kwargs)
        assert error.value.code is ProposalErrorCode.MALFORMED_OUTPUT

    missing = _proposal(analysis_dataset, analysis_column_ids)
    del missing["table_id"]
    with pytest.raises(PlanProposalError, match="missing required fields"):
        parse_plan_proposal(missing, **kwargs)

    extra = _proposal(analysis_dataset, analysis_column_ids, python="print(1)")
    with pytest.raises(PlanProposalError, match="unsupported fields"):
        parse_plan_proposal(extra, **kwargs)

    expression = _proposal(
        analysis_dataset,
        analysis_column_ids,
        steps=[
            {
                "type": "filter",
                "column_id": analysis_column_ids["region"],
                "operator": "EQ",
                "value": "North",
                "expression": "df[df.amount > 100]",
            }
        ],
    )
    with pytest.raises(PlanProposalError, match="unsupported fields"):
        parse_plan_proposal(expression, **kwargs)

    too_large = " " * MAX_PROPOSAL_BYTES + "{}"
    with pytest.raises(PlanProposalError, match="size limit"):
        parse_plan_proposal(too_large, **kwargs)


def test_parser_classifies_unsupported_ops_and_preserves_unknown_ids_for_validator(
    analysis_dataset, analysis_column_ids
) -> None:
    context = build_catalog_context(analysis_dataset)
    kwargs = {
        "question_id": "question-1",
        "dataset": analysis_dataset,
        "context": context,
    }
    unsupported = _proposal(
        analysis_dataset,
        analysis_column_ids,
        steps=[{"type": "join", "left_table_id": "a", "right_table_id": "b"}],
    )
    with pytest.raises(PlanProposalError) as error:
        parse_plan_proposal(unsupported, **kwargs)
    assert error.value.code is ProposalErrorCode.UNSUPPORTED_OPERATION

    unknown_column = _proposal(
        analysis_dataset,
        analysis_column_ids,
        steps=[
            {
                "type": "filter",
                "column_id": "missing-column",
                "operator": "EQ",
                "value": "x",
            }
        ],
    )
    parsed = parse_plan_proposal(unknown_column, **kwargs)
    assert parsed.plan.steps[1].column_id == "missing-column"


def test_expression_like_literal_remains_data_until_deterministic_validation(
    analysis_dataset, analysis_column_ids
) -> None:
    hostile = "__import__('os').system('whoami')"
    proposal = _proposal(
        analysis_dataset,
        analysis_column_ids,
        steps=[
            {
                "type": "filter",
                "column_id": analysis_column_ids["region"],
                "operator": "EQ",
                "value": hostile,
            }
        ],
    )
    parsed = parse_plan_proposal(
        proposal,
        question_id="question-1",
        dataset=analysis_dataset,
        context=build_catalog_context(analysis_dataset),
    )

    assert parsed.plan.steps[1].value == hostile
    assert isinstance(parsed.plan.steps[1].value, str)


def test_parser_supports_explicit_typed_datetime_filter_literals(
    multi_sheet_xlsx_bytes,
) -> None:
    dataset = ingest_upload("book.xlsx", multi_sheet_xlsx_bytes)
    table = next(
        ref for ref in dataset.manifest.tables if ref.display_name == "Inventory"
    )
    context = build_catalog_context(dataset)
    created_column = next(
        column.column_id
        for catalog_table in context.tables
        if catalog_table.table_id == table.table_id
        for column in catalog_table.columns
        if column.name == "created"
    )
    proposal = {
        "table_id": table.table_id,
        "required_fields": ["created"],
        "steps": [
            {
                "type": "filter",
                "column_id": created_column,
                "operator": "EQ",
                "value": {"type": "datetime", "value": "2025-01-02T00:00:00"},
            }
        ],
    }

    parsed = parse_plan_proposal(
        proposal,
        question_id="date-question",
        dataset=dataset,
        context=context,
    )

    assert parsed.plan.steps[1].value == datetime(2025, 1, 2)
