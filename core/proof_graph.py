"""Deterministic typed provenance graph construction and serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum

from core.evidence import EvidenceOperationKind, EvidenceRecord
from core.schema import stable_id

PROOF_GRAPH_VERSION = "1.0"


class ProofNodeType(str, Enum):
    QUESTION = "QUESTION"
    DATASET = "DATASET"
    TABLE = "TABLE"
    COLUMN = "COLUMN"
    FILTER = "FILTER"
    CALCULATION = "CALCULATION"
    TRANSFORMATION = "TRANSFORMATION"
    RESULT = "RESULT"
    VERIFICATION = "VERIFICATION"


class ProofEdgeType(str, Enum):
    USES = "USES"
    FILTERED_BY = "FILTERED_BY"
    CALCULATES = "CALCULATES"
    TRANSFORMS = "TRANSFORMS"
    PRODUCES = "PRODUCES"
    VERIFIED_BY = "VERIFIED_BY"


@dataclass(frozen=True)
class ProofNode:
    node_id: str
    node_type: ProofNodeType
    label: str
    metadata: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ProofEdge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: ProofEdgeType


@dataclass(frozen=True)
class ProofGraph:
    graph_id: str
    version: str
    evidence_id: str
    nodes: tuple[ProofNode, ...]
    edges: tuple[ProofEdge, ...]


def build_proof_graph(evidence: EvidenceRecord) -> ProofGraph:
    """Build a graph only from operations and lineage recorded in evidence."""
    if type(evidence) is not EvidenceRecord:
        raise TypeError("Proof graph construction requires an EvidenceRecord.")
    key = evidence.evidence_id
    nodes: list[ProofNode] = []
    edges: list[ProofEdge] = []

    question_id = _node_id(key, "question")
    dataset_id = _node_id(key, "dataset")
    question = _node(
        question_id,
        ProofNodeType.QUESTION,
        evidence.question,
        (("question_id", evidence.question_id),),
    )
    dataset = _node(
        dataset_id,
        ProofNodeType.DATASET,
        evidence.dataset_id,
        (
            ("dataset_id", evidence.dataset_id),
            ("sha256", evidence.dataset_sha256),
        ),
    )
    nodes.extend((question, dataset))
    _edge(edges, key, question_id, dataset_id, ProofEdgeType.USES, 0)

    table_nodes: list[tuple[str, str]] = []
    for index, table in enumerate(evidence.source_tables):
        node_id = _node_id(key, f"table:{table.table_id}")
        nodes.append(
            _node(
                node_id,
                ProofNodeType.TABLE,
                table.display_name,
                (
                    ("table_id", table.table_id),
                    ("source_file_id", table.source_file_id),
                    ("sheet_name", table.sheet_name),
                    ("row_count", table.row_count),
                    ("column_count", table.column_count),
                ),
            )
        )
        _edge(edges, key, dataset_id, node_id, ProofEdgeType.USES, index + 1)
        table_nodes.append((table.table_id, node_id))

    column_nodes: dict[str, str] = {}
    edge_index = 100
    for column in evidence.source_columns:
        node_id = _node_id(key, f"column:{column.column_id}")
        column_nodes[column.column_id] = node_id
        nodes.append(
            _node(
                node_id,
                ProofNodeType.COLUMN,
                column.name,
                (
                    ("column_id", column.column_id),
                    ("table_id", column.table_id),
                    ("index", column.index),
                    ("source_file_id", column.source_file_id),
                    ("sheet_name", column.sheet_name),
                ),
            )
        )
        parent_id = next(
            (node for table_id, node in table_nodes if table_id == column.table_id),
            None,
        )
        if parent_id is not None:
            _edge(
                edges,
                key,
                parent_id,
                node_id,
                ProofEdgeType.USES,
                edge_index,
            )
            edge_index += 1

    predecessor = table_nodes[0][1] if table_nodes else dataset_id
    for operation_index, operation in enumerate(evidence.operations):
        operation_key = f"operation:{operation.step_index}:{operation.kind.value}"
        node_id = _node_id(key, operation_key)
        node_type = {
            EvidenceOperationKind.FILTER: ProofNodeType.FILTER,
            EvidenceOperationKind.GROUP_AGGREGATE: ProofNodeType.CALCULATION,
            EvidenceOperationKind.SORT: ProofNodeType.TRANSFORMATION,
            EvidenceOperationKind.LIMIT: ProofNodeType.TRANSFORMATION,
            EvidenceOperationKind.PROJECT: ProofNodeType.TRANSFORMATION,
        }[operation.kind]
        label = operation.kind.value.replace("_", " ").title()
        metadata = (
            ("step_index", operation.step_index),
            ("column_ids", operation.column_ids),
            ("operator", operation.operator),
            ("value", operation.value),
            ("aggregation", operation.aggregation),
            ("output_name", operation.output_name),
            ("limit", operation.limit),
            ("ascending", operation.ascending),
            ("output_names", operation.output_names),
        )
        nodes.append(_node(node_id, node_type, label, metadata))
        relation = (
            ProofEdgeType.FILTERED_BY
            if operation.kind is EvidenceOperationKind.FILTER
            else ProofEdgeType.CALCULATES
            if operation.kind is EvidenceOperationKind.GROUP_AGGREGATE
            else ProofEdgeType.TRANSFORMS
        )
        _edge(edges, key, predecessor, node_id, relation, operation_index)
        predecessor = node_id
        for column_id in operation.column_ids:
            source_column = column_nodes.get(column_id)
            if source_column is not None:
                _edge(
                    edges,
                    key,
                    source_column,
                    node_id,
                    ProofEdgeType.FILTERED_BY
                    if operation.kind is EvidenceOperationKind.FILTER
                    else ProofEdgeType.USES,
                    edge_index,
                )
                edge_index += 1

    result_node_id = _node_id(key, "result")
    result_metadata = (
        ("row_count", evidence.result.row_count if evidence.result else None),
        ("columns", evidence.result.columns if evidence.result else ()),
        (
            "lineage_row_count",
            evidence.result.lineage_row_count if evidence.result else 0,
        ),
        (
            "content_sha256",
            evidence.result.content_sha256 if evidence.result else None,
        ),
    )
    nodes.append(
        _node(
            result_node_id,
            ProofNodeType.RESULT,
            "Analysis result" if evidence.result is not None else "No result produced",
            result_metadata,
        )
    )
    if evidence.result is not None:
        _edge(
            edges,
            key,
            predecessor,
            result_node_id,
            ProofEdgeType.PRODUCES,
            len(evidence.operations),
        )

    verification_node_id = _node_id(key, "verification")
    nodes.append(
        _node(
            verification_node_id,
            ProofNodeType.VERIFICATION,
            evidence.verification.status.value.replace("_", " ").title(),
            (
                ("status", evidence.verification.status.value),
                ("verifier_version", evidence.verification.verifier_version),
                ("checks", tuple(check.name for check in evidence.verification.checks)),
                ("reasons", evidence.verification.reasons),
            ),
        )
    )
    _edge(
        edges,
        key,
        result_node_id,
        verification_node_id,
        ProofEdgeType.VERIFIED_BY,
        len(evidence.operations) + 1,
    )

    return ProofGraph(
        graph_id=stable_id("proof-graph", key),
        version=PROOF_GRAPH_VERSION,
        evidence_id=key,
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def proof_graph_to_dict(graph: ProofGraph) -> dict[str, object]:
    """Serialize a graph into stable JSON-compatible built-in values."""
    if type(graph) is not ProofGraph:
        raise TypeError("Only a ProofGraph can be serialized.")
    return {
        "graph_id": graph.graph_id,
        "version": graph.version,
        "evidence_id": graph.evidence_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "node_type": node.node_type.value,
                "label": node.label,
                "metadata": {
                    key: _json_value(value)
                    for key, value in sorted(node.metadata, key=lambda pair: pair[0])
                },
            }
            for node in sorted(graph.nodes, key=lambda item: item.node_id)
        ],
        "edges": [
            {
                "edge_id": edge.edge_id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "edge_type": edge.edge_type.value,
            }
            for edge in sorted(graph.edges, key=lambda item: item.edge_id)
        ],
    }


def serialize_proof_graph(graph: ProofGraph) -> str:
    """Return deterministic JSON for a proof graph."""
    return json.dumps(
        proof_graph_to_dict(graph),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _node_id(evidence_id: str, identity: str) -> str:
    return stable_id("proof-node", f"{evidence_id}\0{identity}")


def _node(
    node_id: str,
    node_type: ProofNodeType,
    label: str,
    metadata: tuple[tuple[str, object], ...],
) -> ProofNode:
    return ProofNode(node_id, node_type, label, metadata)


def _edge(
    edges: list[ProofEdge],
    evidence_id: str,
    source_id: str,
    target_id: str,
    edge_type: ProofEdgeType,
    ordinal: int,
) -> None:
    edge_id = stable_id(
        "proof-edge",
        f"{evidence_id}\0{ordinal}\0{source_id}\0{target_id}\0{edge_type.value}",
    )
    edges.append(ProofEdge(edge_id, source_id, target_id, edge_type))


def _json_value(value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("Non-finite graph metadata cannot be serialized.")
        return value
    if isinstance(value, Enum):
        return value.value
    if type(value) in {datetime, date, time}:
        return {"$type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(value[key]) for key in sorted(value)}
    raise TypeError(f"Unsupported graph metadata value: {type(value).__name__}.")
