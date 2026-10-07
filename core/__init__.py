"""Deterministic data foundation for ProofIQ."""

from core.agent import (
    Agent,
    AgentFailure,
    AgentFailureCode,
    AgentStatus,
    AnalysisOutcome,
)
from core.answerability import assess_answerability
from core.code_renderer import format_python_literal, render_plan_code
from core.evidence import (
    EvidenceRecord,
    build_evidence,
    evidence_to_dict,
    serialize_evidence,
)
from core.executor import ExecutionError, execute_plan
from core.ingestion import DEFAULT_INGESTION_LIMITS, ingest_upload
from core.llm_client import (
    CatalogContext,
    LLMClient,
    LLMClientError,
    LLMTimeoutError,
    LLMUnavailableError,
    MockLLMClient,
    PlanProposalError,
    build_catalog_context,
    parse_plan_proposal,
)
from core.planner import validate_plan
from core.profiler import profile_dataset
from core.proof_graph import (
    ProofEdge,
    ProofEdgeType,
    ProofGraph,
    ProofNode,
    ProofNodeType,
    build_proof_graph,
    proof_graph_to_dict,
    serialize_proof_graph,
)
from core.verifier import verify_result

__all__ = [
    "Agent",
    "AgentFailure",
    "AgentFailureCode",
    "AgentStatus",
    "AnalysisOutcome",
    "build_evidence",
    "build_proof_graph",
    "build_catalog_context",
    "CatalogContext",
    "DEFAULT_INGESTION_LIMITS",
    "ExecutionError",
    "EvidenceRecord",
    "ProofEdge",
    "ProofEdgeType",
    "ProofGraph",
    "ProofNode",
    "ProofNodeType",
    "assess_answerability",
    "evidence_to_dict",
    "execute_plan",
    "format_python_literal",
    "ingest_upload",
    "LLMClient",
    "LLMClientError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "MockLLMClient",
    "parse_plan_proposal",
    "PlanProposalError",
    "profile_dataset",
    "proof_graph_to_dict",
    "render_plan_code",
    "serialize_evidence",
    "serialize_proof_graph",
    "validate_plan",
    "verify_result",
]
