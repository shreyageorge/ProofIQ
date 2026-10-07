"""Deterministic provenance graph tests."""

import json

from core.evidence import build_evidence
from core.executor import execute_plan
from core.planner import validate_plan
from core.proof_graph import (
    ProofEdgeType,
    ProofNodeType,
    build_proof_graph,
    proof_graph_to_dict,
    serialize_proof_graph,
)
from core.verifier import verify_result
from models.schemas import (
    Aggregation,
    AggregationFunction,
    AnalysisPlan,
    Filter,
    FilterOperator,
    GroupAggregate,
    Limit,
    QuestionRequest,
    SelectTable,
    Sort,
    SortKey,
    VerificationReport,
    VerificationStatus,
)


def test_graph_contains_only_executed_plan_steps_and_stable_lineage(
    analysis_dataset, analysis_column_ids
) -> None:
    ids = analysis_column_ids
    plan = AnalysisPlan(
        "graph-plan",
        "graph-question",
        analysis_dataset.manifest.dataset_id,
        (
            SelectTable(analysis_dataset.tables[0].table_id),
            Filter(ids["amount"], FilterOperator.GT, 100),
            GroupAggregate(
                (ids["region"],),
                Aggregation(AggregationFunction.SUM, ids["amount"]),
            ),
            Sort((SortKey(ascending=False, output_name="value"),)),
            Limit(1),
        ),
    )
    validation = validate_plan(analysis_dataset, plan)
    assert validation.accepted, validation.issues
    result = execute_plan(analysis_dataset, validation)
    verification = verify_result(analysis_dataset, validation, result)
    evidence = build_evidence(
        QuestionRequest(
            "graph-question",
            "Total amount by region above 100.",
            analysis_dataset.manifest.dataset_id,
        ),
        analysis_dataset,
        validation,
        result,
        verification,
    )

    graph = build_proof_graph(evidence)
    repeated = build_proof_graph(evidence)
    types = [node.node_type for node in graph.nodes]

    assert graph == repeated
    assert types.count(ProofNodeType.TABLE) == 1
    assert types.count(ProofNodeType.FILTER) == 1
    assert types.count(ProofNodeType.CALCULATION) == 1
    assert types.count(ProofNodeType.TRANSFORMATION) == 2
    assert types.count(ProofNodeType.RESULT) == 1
    assert types.count(ProofNodeType.VERIFICATION) == 1
    assert not any(node.node_type.value == "JOIN" for node in graph.nodes)
    assert ProofEdgeType.FILTERED_BY in {edge.edge_type for edge in graph.edges}
    assert ProofEdgeType.CALCULATES in {edge.edge_type for edge in graph.edges}
    assert ProofEdgeType.PRODUCES in {edge.edge_type for edge in graph.edges}
    assert ProofEdgeType.VERIFIED_BY in {edge.edge_type for edge in graph.edges}

    serialized = serialize_proof_graph(graph)
    assert serialized == serialize_proof_graph(repeated)
    assert json.loads(serialized) == proof_graph_to_dict(graph)
    assert "North" not in serialized


def test_graph_omits_nonexistent_filter_nodes(analysis_dataset) -> None:
    plan = AnalysisPlan(
        "unfiltered-plan",
        "unfiltered-question",
        analysis_dataset.manifest.dataset_id,
        (SelectTable(analysis_dataset.tables[0].table_id),),
    )
    validation = validate_plan(analysis_dataset, plan)
    result = execute_plan(analysis_dataset, validation)
    verification = verify_result(analysis_dataset, validation, result)
    evidence = build_evidence(
        QuestionRequest(
            "unfiltered-question",
            "Show the uploaded table.",
            analysis_dataset.manifest.dataset_id,
        ),
        analysis_dataset,
        validation,
        result,
        verification,
    )

    graph = build_proof_graph(evidence)

    assert not any(node.node_type is ProofNodeType.FILTER for node in graph.nodes)
    assert len(evidence.operations) == 0
    assert any(edge.edge_type is ProofEdgeType.PRODUCES for edge in graph.edges)


def test_graph_does_not_claim_a_result_for_failed_execution(analysis_dataset) -> None:
    plan = AnalysisPlan(
        "failed-graph-plan",
        "failed-graph-question",
        analysis_dataset.manifest.dataset_id,
        (SelectTable(analysis_dataset.tables[0].table_id),),
    )
    validation = validate_plan(analysis_dataset, plan)
    report = VerificationReport(
        VerificationStatus.NOT_VERIFIED,
        (),
        ("Execution was unavailable.",),
        "1.0",
    )
    evidence = build_evidence(
        QuestionRequest(
            "failed-graph-question",
            "Show the uploaded table.",
            analysis_dataset.manifest.dataset_id,
        ),
        analysis_dataset,
        validation,
        None,
        report,
        execution_error="Execution failed.",
    )

    graph = build_proof_graph(evidence)
    result_node = next(
        node for node in graph.nodes if node.node_type is ProofNodeType.RESULT
    )

    assert result_node.label == "No result produced"
    assert ProofEdgeType.PRODUCES not in {edge.edge_type for edge in graph.edges}
