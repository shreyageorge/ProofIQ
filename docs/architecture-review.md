# ProofIQ Architecture Review

## Executive assessment

The source architecture captures the product's most important trust requirements: separate planning from execution and verification, preserve provenance, and admit when the data is insufficient. It is a sound direction, not yet an implementation contract. It leaves the plan language, answerability criteria, data limits, join semantics, execution boundary, and meaning of “verified” underspecified. The largest safety issue is the combination of LLM-generated Python and a vaguely “controlled” executor: a timeout and a restricted `globals` dictionary do not sandbox Python.

**Recommendation:** make a small typed analysis plan the only executable authority. Validate it, then interpret its finite set of operations in trusted Python/Pandas code. Generate readable Python from the validated plan for the evidence/code viewer, but do not run arbitrary LLM-authored source in the MVP. Make answerability/schema validation and result computation deterministic. Use an LLM only to propose a plan and explain already-computed results, with strict schema validation and no state-changing tools.

## Assessment of the current proposal

### Strengths

- Correctly prioritizes reproducibility, refusal when data is insufficient, and independent verification.
- Identifies useful component boundaries and phases the work rather than implementing everything at once.
- Names provenance, a proof graph, execution capture, and verification as first-class product concepts.
- Avoids giving the LLM direct application-state control.

### Gaps, contradictions, and assumptions to resolve

1. **“Generate code” vs. safe execution:** executing model-produced Python while preventing computer access is not achieved by code review, `exec` globals, a timeout, or a subprocess alone. The MVP should execute a constrained plan, not arbitrary source. Treat displayed code as deterministic compiler output and state clearly what engine actually ran.
2. **Plan example is not a safe or complete schema:** strings such as `quarter == 'Q3'` and `sum(amount)` are executable-expression-shaped, ambiguous, and hard to validate. Represent filters, aggregations, joins, and sort order as typed enum/operator/value fields; reject unknown fields and operators.
3. **Answerability is not assigned to a trustworthy owner:** an LLM can suggest missing fields but cannot establish their existence. A deterministic gate must resolve every referenced table/column and required operation against the inspected catalog before execution. Ambiguity or missing data must become clarification, partial answer with explicit scope, or `CANNOT_DETERMINE`—never an invented field or value.
4. **Join inference can silently corrupt results:** guessed keys and many-to-many joins may multiply rows. Require explicit, validated join keys and cardinality expectations; detect duplicate keys and unexpected row expansion. Do not auto-join ambiguous tables in the MVP.
5. **“Independent recomputation” is vague:** repeating the same function or trusting the same LLM is not independent. Keep a separately implemented verifier for the supported aggregation subset, compare against execution output, and report exactly what was checked. Do not label a result `VERIFIED` if the check was skipped, unsupported, timed out, or disagreed.
6. **Data profiling can expose private data:** profiles and prompts should prefer schema, counts, and bounded/redacted examples. Never transmit complete tables to the LLM by default. Make any sample transmission deliberate and disclose it.
7. **Input/data constraints are absent:** set file, workbook, sheet, row, column, cell, and runtime limits; reject unsupported formats and malformed/oversized inputs with actionable errors. Define null, duplicate, date, numeric, locale, and mixed-type behavior rather than silently coercing.
8. **Evidence lineage needs stable identifiers:** column names alone do not identify the exact input version or rows used. Fingerprint uploaded file bytes and assign table/row identifiers; preserve source-column lineage through filters, projections, joins, and aggregations. Bound UI evidence payloads without discarding the underlying provenance needed for recomputation.
9. **Status vocabulary and product wording need precision:** distinguish answerability (`ANSWERABLE`, `PARTIALLY_ANSWERABLE`, `CANNOT_DETERMINE`) from verification (`VERIFIED`, `PARTIALLY_VERIFIED`, `FAILED`, preferably `NOT_VERIFIED`). A successful execution is not proof. An answerable plan can still fail verification.
10. **UI/architecture stack has a small mismatch:** Plotly is named in the visualization component but not the technology stack. Include it only if a chart is needed for the demo. Keep deployment, persistent storage, background job systems, and a general agent framework out of the first MVP.
11. **Phase ordering should put contracts and deterministic correctness before the LLM/UI:** establish typed models, ingestion, plan validation, execution, and verifier tests before connecting an LLM or presenting verified results.

## Recommended component architecture

Use ordinary Python modules and typed boundaries. Start with standard-library dataclasses and enums for internal contracts; validate external LLM JSON explicitly at the adapter boundary. Pydantic is an optional choice only if it is already available or deliberately added later. Keep each layer callable without Streamlit or an LLM so it can be unit-tested.

| Component | Responsibilities | Must not do |
|---|---|---|
| UI (`app.py`) | Upload, session/workspace selection, question entry, progress, rendering answer/code/evidence/proof graph/chart, display errors and status | Calculate answers, decide verification status, execute source text, pass mutable UI state to an LLM |
| Ingestion | Validate file type/size; load CSV/XLSX and selected sheets; assign stable dataset/table/row IDs; retain immutable uploaded snapshot/fingerprint | Infer question intent, silently drop rows/columns, execute macros/formulas, trust client filenames |
| Profiler/catalog | Produce column types, null/duplicate counts, bounded categorical summaries, date/numeric hints, candidate keys/relationships with confidence | Treat heuristic relationships as confirmed joins; send full data to model |
| Interpretation adapter | Translate user question to a typed intent/plan proposal; return schema-constrained data and uncertainty | Call tools, mutate application state, perform arithmetic, access data files or emit executable expressions |
| Answerability/plan validator | Resolve proposed references against catalog; enforce supported operations, limits, join rules, types and assumptions; request clarification or refuse when needed | Guess missing columns, approve arbitrary expressions, let model decision alone authorize execution |
| Plan engine | Execute only validated, allow-listed plan operations on in-memory approved tables using deterministic Pandas functions | `eval`, `exec`, shell, imports, filesystem/network access, arbitrary callbacks from plan |
| Python/code renderer | Render reproducible, readable Python from a validated plan and fixed templates; bind values safely and include dataset fingerprint | Execute LLM-supplied code or use generated code as source of truth for the result |
| Verifier | Independently recompute supported metrics and invariants; compare execution result with documented tolerances; set verification state | Trust model assertions, execution output alone, or a successful process exit as proof |
| Evidence/proof graph | Assemble immutable result, plan, dataset fingerprint, columns, row lineage/counts, assumptions, limitations, code, verifier report and graph edges | Claim proof of intent or causality beyond what data and checks establish |
| Chart builder | Select supported chart from result shape or render a user-selected supported chart; derive values only from result | Ask an LLM to invent chart data or affect numeric answer |
| LLM provider adapter | Make a bounded request using schema/profile context; validate response, handle timeout/rate/error, avoid logging secrets | Receive unrestricted data/filesystem tools, hold authoritative app state, or see credentials in prompts/logs |

## Typed interfaces (proposed contract)

The following is a language-level design, not application code. Use frozen/immutable dataclasses or equivalent models, explicit enums, and validation at every trust boundary. IDs are opaque strings; source row references should be compact ranges/sets as appropriate.

- `DatasetManifest`: `dataset_id`, `content_sha256`, `original_name`, `format`, `tables: tuple[TableRef, ...]`, `ingested_at`, `limits_applied`.
- `TableRef`: `table_id`, `display_name`, `row_count`, `columns: tuple[ColumnProfile, ...]`, `source_file_id`, `source_sheet`, `row_id_column` (internal, not a user column).
- `ColumnProfile`: `column_id`, `display_name`, `physical_type`, `semantic_hints`, `null_count`, `distinct_count`, bounded/redacted `sample_values`, `source_lineage`.
- `QuestionRequest`: `question_id`, `text`, `dataset_id`, optional `locale`.
- `Answerability`: enum `ANSWERABLE | PARTIALLY_ANSWERABLE | CANNOT_DETERMINE | NEEDS_CLARIFICATION`; `reasons: tuple[Reason, ...]`; `missing_fields`, `assumptions`, `limitations`. Gate output comes from deterministic validation; model suggestions are inputs only.
- `AnalysisPlan`: `plan_id`, `question_id`, `dataset_id`, `steps: tuple[PlanStep, ...]`, `output_spec`, `assumptions`, `plan_version`. `PlanStep` is a tagged union of `SelectTable`, `Filter`, `Join`, `Derive`, `GroupAggregate`, `Sort`, `Limit`, and `Project`; arguments are typed fields, not expression strings.
- `Filter`: column ID, allow-listed operator (`EQ`, `IN`, `GT`, `GTE`, `LT`, `LTE`, `IS_NULL`, `CONTAINS` only if explicitly supported), typed literal(s), and explicit null behavior. `Aggregate`: allow-listed function (`SUM`, `COUNT`, `COUNT_DISTINCT`, `MEAN`, `MIN`, `MAX`) and column ID or explicit row-count target. Reject unsupported metrics/units.
- `PlanValidation`: `accepted`, validated/resolved plan or `None`, `issues`, `answerability`; no execution occurs for a rejected plan.
- `ExecutionResult`: `run_id`, `plan_id`, `status`, typed result table/scalars, `row_count`, `duration_ms`, `warnings`, `lineage`, `error_code`; never expose traceback/secrets as user-facing data.
- `VerificationReport`: `status: VERIFIED | PARTIALLY_VERIFIED | FAILED | NOT_VERIFIED`, checks with expected/observed/tolerance and evidence references, reason, verifier version. Define `VERIFIED` narrowly for explicitly supported checks.
- `EvidenceRecord`: `question`, manifest fingerprint, validated plan, execution result, source columns, filters/joins/transformations, row counts/source references, generated code, verification report, limitations, timestamps and component versions.
- `ProofGraph`: typed nodes (`QUESTION`, `DATASET`, `TABLE`, `COLUMN`, `FILTER`, `JOIN`, `TRANSFORMATION`, `CALCULATION`, `RESULT`, `VERIFICATION`) and typed directed edges with evidence references. It is a rendering of the evidence record, not a separate mutable truth source.

A pure orchestration function can compose these boundaries conceptually:

`analyze(request: QuestionRequest, manifest: DatasetManifest, tables: ReadOnlyTableStore, planner: Planner) -> AnalysisOutcome`

`AnalysisOutcome` contains answerability, optional validated plan, optional execution result, verification report, evidence, and user-facing explanation. The orchestrator enforces the order and short-circuits before execution on refusal, ambiguity, or validation failure. Use typed `Result`/domain errors rather than leaking raw exceptions across layers.

### Minimum plan expressiveness for a credible MVP

Support one-table filters, group-by, a single aggregation, sort/rank, and limit. Add joins only after explicit key selection and cardinality checks are tested. Do not support arbitrary formulas, Python expressions, SQL, pivoting, fuzzy joins, or unrestricted natural-language-to-code repair in the first demo. For a question asking for a concept not represented in the catalog (for example, “profit” when only revenue exists), return `CANNOT_DETERMINE` or ask a clarifying question rather than inventing a derivation.

## Data flow

1. UI accepts bounded uploads and question text; backend creates an immutable dataset snapshot and content fingerprint.
2. Ingestion validates formats/sizes and loads CSV/XLSX sheets into internal tables with source file/sheet and stable row IDs.
3. Profiler builds a catalog. It may propose semantic types and relationships, each tagged as heuristic and unconfirmed.
4. Planner receives question plus schema/profile metadata and only the minimum necessary, bounded/redacted values. It returns structured JSON plan/clarification; no tool calls and no arbitrary source code.
5. Deterministic validator resolves IDs, checks column/type/operator validity, supported scope, assumptions, and join cardinality. It decides whether execution may proceed.
6. Deterministic plan engine executes allow-listed steps over read-only in-memory tables, collecting row counts, lineage and warnings.
7. Independent verifier recomputes supported aggregates/checks, compares outputs, and emits a status. A disagreement or unsupported check prevents `VERIFIED`.
8. Evidence builder freezes the entire derivation. Code renderer emits reproducible Python from the accepted plan; this source is not executed in the MVP.
9. Optional LLM narration receives only the verified result and safe evidence summary; it cannot change numbers/status. UI displays status and limitations adjacent to the answer. Charts are derived from result data.

## Deterministic vs. LLM-eligible work

**Deterministic and authoritative:** file validation/loading; row IDs and fingerprints; schema catalog facts and counts; plan schema/ID/type checks; answerability decisions based on available fields and supported operations; all filtering, joining, aggregation, sorting and ranking; numerical formatting; verification and status; provenance/evidence/proof graph; chart data; code rendering; limits and error policy.

**LLM may propose, never authorize:** intent interpretation, candidate table/column mapping, operation sequence, possible clarification questions, and concise prose summarization of computed results. Require JSON/schema-constrained output, low context, timeout/retry limits, no function/tool execution, and post-validation. Treat every LLM field as untrusted. If no model is configured, deterministic demo plans/fixtures or a transparent “planner unavailable” response should work.

## Trust boundaries and security risks

- **Uploaded files → parser:** malicious/oversized files, malformed CSV, workbook resource exhaustion, formula content, path/name tricks, zip bombs. Accept a narrow allow-list; cap compressed and expanded size, sheets, rows/columns/cells and processing time; use generated internal names; never execute macros or formulas; handle parser errors safely.
- **Application → LLM provider:** uploaded content may contain personal, confidential, or prompt-injection text. Minimize and redact context, do not send complete rows by default, treat cell text as data rather than instructions, never include secrets, disclose external processing, and make provider calls opt-in/configured.
- **LLM response → validator:** malformed/extra JSON, prompt injection, nonexistent columns, pathological plan size, unsupported operators, and coercion attacks. Enforce strict schema, size/depth/step limits, exact identifier resolution, typed literals and allow-lists; reject, do not repair by executing prose.
- **Plan engine → data:** plans can consume excessive memory/CPU or cause join explosion. Bound rows, steps, intermediate result size and runtime; reject uncontrolled joins; avoid mutating source frames and copy/operate on scoped frames.
- **Generated Python → host:** arbitrary Python can import modules, read credentials/files, use sockets/subprocesses, exfiltrate data, consume resources, or exploit dependencies. Do not execute LLM-generated code. Restrict MVP to fixed deterministic plan operations. A subprocess, `exec` namespace, AST allow-list, or timeout is not a robust sandbox. If literal code execution becomes mandatory, use a separate disposable, unprivileged OS/container sandbox with no network, read-only minimal inputs, resource limits, kill timeout, and no secrets mounted; validate this isolation for the actual deployment platform before trusting it.
- **UI/session → shared state:** cross-user/session data leakage and stale results. Use per-session immutable snapshots, avoid global mutable DataFrames, bind every run to dataset fingerprint and question/plan IDs, clear state on replacement, and avoid logging row contents.
- **Secrets/logging/dependencies:** read API keys only from environment or Streamlit secrets; never commit them or include them in client bundles, prompts, tracebacks, or evidence. Pin/review dependencies and avoid storing raw sensitive uploads in logs/artifacts.

## Execution and correctness risks

- Never use `eval`, `exec`, shell commands, or dynamic imports on user/model input. Do not allow a repair loop to rewrite/re-execute arbitrary source. If a plan fails, return a bounded diagnostic; permit at most a structured re-plan through normal validation, not silent mutation.
- Set explicit upload and plan limits; use execution time budgets and predictable error handling. In-process timeout cannot safely interrupt arbitrary Python, another reason to keep MVP operations finite and trusted.
- Preserve a distinction among parser errors, invalid plans, unsupported semantics, execution failures, verifier disagreement, and provider errors.
- Specify numeric precision/tolerance, NaN/null behavior, date parsing/time zone, text collation, and tie behavior for rankings. Do not silently treat missing values as zero or parse ambiguous dates as certain.
- A row lineage list can be very large. Store source row IDs or compact references/counts for proof, bound what is rendered, and disclose any truncation.
- Hash exact source bytes and version the plan, engine, and verifier so results can be reproduced against the same input and semantics.

## Verification strategy

1. For every executed plan, independently validate that output schema, row counts, group keys, and sort/rank properties conform to the plan.
2. Independently recompute supported scalar/group aggregates from the validated source rows in a separate verifier path, not by calling the executor's aggregation function. Compare exact counts and keys; use documented absolute/relative tolerances for floating-point metrics; handle nulls explicitly.
3. Add invariants appropriate to each supported operation (e.g., count bounds; filtered rows do not exceed input; one-to-one joins do not unexpectedly multiply rows; grouped row-count reconciliation where semantics permit).
4. Record each check and inputs/tolerances. `FAILED` on mismatch; `PARTIALLY_VERIFIED` when only a defined subset is checked; `NOT_VERIFIED` when checks cannot run. Never downgrade a failure into verified prose.
5. Keep verification deterministic and independent of the LLM. The LLM may word the result only after status is fixed; numeric result, code, evidence, and status displayed must match the same immutable run.
6. State the limits of proof: verification establishes the result under the recorded data, plan, parsing, and assumptions; it does not prove the source data is true, causal claims, or that the plan captured the user's intent.

## Recommended changes to the source architecture

- Replace string-valued filters/metrics with a typed, versioned plan DSL and validator.
- Make deterministic answerability a precondition to execution; add `NEEDS_CLARIFICATION` and `NOT_VERIFIED` where useful.
- In the MVP, interpret plans using trusted Pandas operations and render (rather than execute) equivalent Python from templates. Explicitly defer arbitrary generated-code execution and unrestricted repair loops.
- Define and test CSV/XLSX limits, missing/duplicate/type coercion policy, stable dataset fingerprints, and row/column provenance.
- Keep join/derived-metric support narrow until cardinality and semantic validation are proven.
- Make the verifier separate from the executor and define precisely when `VERIFIED` is permitted.
- Use immutable per-run evidence as the single source of truth for answer, code, proof graph, verification, and chart.
- Start with a deterministic fake planner and fixed fixtures so the app is testable without a network/model key; put any LLM behind an adapter.
- Defer production-grade multi-tenant persistence/deployment and general-purpose sandbox infrastructure until product needs and deployment constraints are known; do not claim arbitrary code is sandboxed in the hackathon MVP.
