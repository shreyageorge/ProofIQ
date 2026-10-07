"""Deterministic orchestration of proposals and trusted ProofIQ components."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.answerability import assess_answerability
from core.evidence import EvidenceRecord, build_evidence
from core.executor import ExecutionError, execute_plan
from core.llm_client import (
    MAX_QUESTION_LENGTH,
    CatalogContextError,
    LLMClient,
    LLMClientError,
    LLMTimeoutError,
    LLMUnavailableError,
    PlanProposalError,
    ProposalErrorCode,
    build_catalog_context,
    parse_plan_proposal,
)
from core.planner import validate_plan
from core.proof_graph import ProofGraph, build_proof_graph
from core.schema import stable_id
from models.schemas import (
    Answerability,
    AnswerabilityStatus,
    ExecutionResult,
    Filter,
    GroupAggregate,
    IngestedDataset,
    PlanValidation,
    Project,
    QuestionRequest,
    ResultTable,
    Sort,
    VerificationReport,
    VerificationStatus,
)
from core.verifier import verify_result


class AgentStatus(str, Enum):
    ANSWERED = "answered"
    PARTIALLY_ANSWERABLE = "partially_answerable"
    NEEDS_CLARIFICATION = "needs_clarification"
    CANNOT_DETERMINE = "cannot_determine"
    FAILED = "failed"
    VERIFICATION_FAILED = "verification_failed"
    PARTIALLY_VERIFIED = "partially_verified"


class AgentFailureCode(str, Enum):
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    LLM_ERROR = "llm_error"
    CONTEXT_TOO_LARGE = "context_too_large"
    MALFORMED_OUTPUT = "malformed_output"
    INVALID_PROPOSAL = "invalid_proposal"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    ANSWERABILITY = "answerability"
    PLAN_REJECTED = "plan_rejected"
    EXECUTION_FAILED = "execution_failed"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


@dataclass(frozen=True, slots=True)
class AgentFailure:
    code: AgentFailureCode
    message: str


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    request: QuestionRequest
    status: AgentStatus
    answerability: Answerability | None = None
    plan_validation: PlanValidation | None = None
    execution: ExecutionResult | None = None
    verification: VerificationReport | None = None
    evidence: EvidenceRecord | None = None
    proof_graph: ProofGraph | None = None
    failure: AgentFailure | None = None

    @property
    def result(self) -> ResultTable | None:
        """Expose an answer table only when the verifier explicitly returned VERIFIED."""
        if (
            self.execution is None
            or self.verification is None
            or self.verification.status is not VerificationStatus.VERIFIED
        ):
            return None
        return self.execution.result

    @property
    def generated_code(self) -> str | None:
        return self.evidence.generated_code if self.evidence is not None else None


ExecutorFunction = Callable[[IngestedDataset, PlanValidation], ExecutionResult]
VerifierFunction = Callable[
    [IngestedDataset, PlanValidation, ExecutionResult], VerificationReport
]


class Agent:
    """Run proposal -> gate -> validator -> executor -> verifier -> evidence."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        timeout_seconds: float = 20.0,
        max_context_bytes: int = 64_000,
        executor: ExecutorFunction = execute_plan,
        verifier: VerifierFunction = verify_result,
    ) -> None:
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number.")
        if type(max_context_bytes) is not int or max_context_bytes < 1:
            raise ValueError("max_context_bytes must be a positive integer.")
        if not callable(getattr(llm_client, "generate_plan", None)):
            raise TypeError("llm_client must implement generate_plan.")
        self._llm_client = llm_client
        self._timeout_seconds = float(timeout_seconds)
        self._max_context_bytes = max_context_bytes
        self._executor = executor
        self._verifier = verifier

    def run(self, question: str, dataset: IngestedDataset) -> AnalysisOutcome:
        """Analyze one question, never executing an unvalidated provider proposal."""
        if type(dataset) is not IngestedDataset:
            raise TypeError("run requires an ingested dataset.")
        if (
            type(question) is not str
            or not question.strip()
            or len(question) > MAX_QUESTION_LENGTH
        ):
            raise ValueError(
                f"question must contain 1 to {MAX_QUESTION_LENGTH} characters."
            )
        request = QuestionRequest(
            question_id=stable_id(
                "question",
                f"{dataset.manifest.content_sha256}\0{question}",
            ),
            text=question,
            dataset_id=dataset.manifest.dataset_id,
        )
        try:
            context = build_catalog_context(
                dataset,
                max_context_bytes=self._max_context_bytes,
            )
        except CatalogContextError:
            return self._failed(
                request,
                AgentFailureCode.CONTEXT_TOO_LARGE,
                "The dataset catalog exceeds the configured proposal context limit.",
            )

        try:
            raw_proposal = self._llm_client.generate_plan(
                question,
                context,
                timeout_seconds=self._timeout_seconds,
            )
        except LLMTimeoutError:
            return self._failed(
                request,
                AgentFailureCode.LLM_TIMEOUT,
                "The plan proposal request timed out.",
            )
        except TimeoutError:
            return self._failed(
                request,
                AgentFailureCode.LLM_TIMEOUT,
                "The plan proposal request timed out.",
            )
        except LLMUnavailableError:
            return self._failed(
                request,
                AgentFailureCode.LLM_UNAVAILABLE,
                "The plan proposal service is unavailable.",
            )
        except LLMClientError:
            return self._failed(
                request,
                AgentFailureCode.LLM_ERROR,
                "The plan proposal request failed.",
            )

        try:
            parsed = parse_plan_proposal(
                raw_proposal,
                question_id=request.question_id,
                dataset=dataset,
                context=context,
            )
        except PlanProposalError as error:
            failure_code = {
                ProposalErrorCode.MALFORMED_OUTPUT: AgentFailureCode.MALFORMED_OUTPUT,
                ProposalErrorCode.UNSUPPORTED_OPERATION: AgentFailureCode.UNSUPPORTED_OPERATION,
                ProposalErrorCode.INVALID_PROPOSAL: AgentFailureCode.INVALID_PROPOSAL,
            }[error.code]
            status = (
                AgentStatus.CANNOT_DETERMINE
                if error.code is ProposalErrorCode.UNSUPPORTED_OPERATION
                else AgentStatus.FAILED
            )
            return self._failed(
                request,
                failure_code,
                str(error),
                status=status,
            )

        answerability = assess_answerability(dataset, parsed.intent)
        if answerability.status not in {
            AnswerabilityStatus.ANSWERABLE,
            AnswerabilityStatus.PARTIALLY_ANSWERABLE,
        }:
            status = (
                AgentStatus.NEEDS_CLARIFICATION
                if answerability.status is AnswerabilityStatus.NEEDS_CLARIFICATION
                else AgentStatus.CANNOT_DETERMINE
            )
            return AnalysisOutcome(
                request=request,
                status=status,
                answerability=answerability,
                failure=AgentFailure(
                    AgentFailureCode.ANSWERABILITY,
                    answerability.reasons[0]
                    if answerability.reasons
                    else "The question cannot be answered from the available catalog.",
                ),
            )
        if answerability.resolved_table_id != parsed.plan.steps[0].table_id:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.CANNOT_DETERMINE,
                answerability=answerability,
                failure=AgentFailure(
                    AgentFailureCode.ANSWERABILITY,
                    "The selected table does not match the deterministic catalog resolution.",
                ),
            )
        required_column_ids = set(
            answerability.resolved_column_ids[: len(parsed.intent.required_fields)]
        )
        if not required_column_ids.issubset(_plan_column_ids(parsed.plan)):
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.CANNOT_DETERMINE,
                answerability=answerability,
                failure=AgentFailure(
                    AgentFailureCode.INVALID_PROPOSAL,
                    "The proposed plan omits one or more required fields.",
                ),
            )

        validation = validate_plan(dataset, parsed.plan)
        if not validation.accepted:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.CANNOT_DETERMINE,
                answerability=answerability,
                plan_validation=validation,
                failure=AgentFailure(
                    AgentFailureCode.PLAN_REJECTED,
                    "The typed plan did not pass deterministic validation.",
                ),
            )

        try:
            execution = self._executor(dataset, validation)
        except ExecutionError:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.FAILED,
                answerability=answerability,
                plan_validation=validation,
                failure=AgentFailure(
                    AgentFailureCode.EXECUTION_FAILED,
                    "Trusted plan execution failed.",
                ),
            )
        verification = self._verifier(dataset, validation, execution)
        evidence = build_evidence(
            request,
            dataset,
            validation,
            execution,
            verification,
            limitations=answerability.limitations,
        )
        graph = build_proof_graph(evidence)

        if verification.status is VerificationStatus.FAILED:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.VERIFICATION_FAILED,
                answerability=answerability,
                plan_validation=validation,
                execution=execution,
                verification=verification,
                evidence=evidence,
                proof_graph=graph,
                failure=AgentFailure(
                    AgentFailureCode.VERIFICATION_FAILED,
                    "Independent verification failed; the result is not presented as verified.",
                ),
            )
        if verification.status is VerificationStatus.NOT_VERIFIED:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.FAILED,
                answerability=answerability,
                plan_validation=validation,
                execution=execution,
                verification=verification,
                evidence=evidence,
                proof_graph=graph,
                failure=AgentFailure(
                    AgentFailureCode.VERIFICATION_UNAVAILABLE,
                    "The result could not be independently verified.",
                ),
            )
        if verification.status is VerificationStatus.PARTIALLY_VERIFIED:
            return AnalysisOutcome(
                request=request,
                status=AgentStatus.PARTIALLY_VERIFIED,
                answerability=answerability,
                plan_validation=validation,
                execution=execution,
                verification=verification,
                evidence=evidence,
                proof_graph=graph,
            )
        status = (
            AgentStatus.PARTIALLY_ANSWERABLE
            if answerability.status is AnswerabilityStatus.PARTIALLY_ANSWERABLE
            else AgentStatus.ANSWERED
        )
        return AnalysisOutcome(
            request=request,
            status=status,
            answerability=answerability,
            plan_validation=validation,
            execution=execution,
            verification=verification,
            evidence=evidence,
            proof_graph=graph,
        )

    @staticmethod
    def _failed(
        request: QuestionRequest,
        code: AgentFailureCode,
        message: str,
        *,
        status: AgentStatus = AgentStatus.FAILED,
    ) -> AnalysisOutcome:
        return AnalysisOutcome(
            request=request,
            status=status,
            failure=AgentFailure(code, message),
        )


def _plan_column_ids(plan) -> set[str]:
    referenced: set[str] = set()
    for step in plan.steps:
        if type(step) is Filter:
            referenced.add(step.column_id)
        elif type(step) is GroupAggregate:
            referenced.update(step.group_by)
            if step.aggregation.column_id is not None:
                referenced.add(step.aggregation.column_id)
        elif type(step) is Sort:
            referenced.update(
                key.column_id for key in step.keys if key.column_id is not None
            )
        elif type(step) is Project:
            referenced.update(step.column_ids)
    return referenced
