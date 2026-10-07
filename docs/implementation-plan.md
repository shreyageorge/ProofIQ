# ProofIQ Implementation Plan

## Goal and scope

Deliver a small, reliable demo that accepts bounded CSV/XLSX inputs, profiles them, interprets a question as a strict structured plan, computes with trusted deterministic Python/Pandas operations, independently checks supported results, and presents answer, reproducible code, evidence, verification status, and a proof graph. Correctly refuse unsupported or unanswerable questions.

**MVP safety decision:** do not execute model-authored Python. Execute only validated allow-listed plan operations through the deterministic engine. Render Python from the validated plan for inspection/reproduction, and label it as generated equivalent code; the trusted plan engine is what produced the displayed result. Arbitrary code execution/isolation is a later, separately approved engineering problem.

## Repository and environment observations

- The repository currently contains `architecture.md`, `.gitignore`, `.vscode/settings.json`, and an untracked `.venv`; there is no application source, dependency manifest, test suite, or committed history yet.
- The checked-in Python environment configuration targets Python 3.12. The workspace `.venv` is Python 3.12 and currently reports only `pip` installed. No dependencies were installed during this review.
- `architecture.md` proposes Streamlit, Pandas, OpenPyXL, Plotly, Pytest, and an LLM API; these are planned choices, not currently available dependencies. Confirm the chosen LLM provider and install dependencies only after implementation is approved.
- The current `.gitignore` excludes `.venv`, `.streamlit`, `.env`, uploaded/private data, and common test artifacts; preserve these protections. Review whether the whole `.streamlit/` exclusion is desired if a non-secret config file will later be committed.

## Milestones (smallest practical sequence)

### Milestone 1 — Contracts, repository foundation, ingestion/profile

**Purpose:** create a testable core and trustworthy data catalog before any model/UI work.

**Expected files:**
- `requirements.txt` (initial minimal dependencies only after approval)
- `README.md` (environment/setup, run/tests, supported input/data limits)
- `models/__init__.py`, `models/schemas.py` (domain enums and typed immutable contracts)
- `core/__init__.py`, `core/ingestion.py`, `core/profiler.py`, `core/schema.py`
- `tests/conftest.py`, `tests/fixtures/` (small synthetic CSV/XLSX fixtures), `tests/test_ingestion.py`, `tests/test_profiler.py`

**Dependencies:** Python 3.12; Pandas; OpenPyXL for XLSX; Pytest for tests. Prefer standard-library dataclasses/enums first; introduce a validation library only if explicitly chosen and installed.

**Tests required:** valid CSV and multiple XLSX sheets; malformed/empty/unsupported inputs; size/row/sheet limits; duplicate headers/rows; nulls, mixed types and ambiguous dates; stable row IDs and byte fingerprint; no macro/formula execution; profiling counts consistent with source.

**Acceptance criteria:** uploads become immutable table snapshots with stable IDs and catalog; limits/errors are explicit; no question or LLM behavior exists yet; all ingestion/profile tests pass without network access.

### Milestone 2 — Typed plan validator, answerability, deterministic engine and verifier

**Purpose:** establish correctness and refusal behavior without an LLM.

**Expected files:**
- `core/planner.py` (plan types/building helpers and strict validation; keep interpretation adapter seam)
- `core/answerability.py` (deterministic resolution and gate)
- `core/executor.py` (allow-listed plan interpreter only)
- `core/verifier.py` (separate recomputation and status rules)
- `tests/test_planner.py`, `tests/test_answerability.py`, `tests/test_executor.py`, `tests/test_verifier.py`

**Dependencies:** Milestone 1; Pandas; Pytest. No LLM package.

**Initial supported scope:** one table, column filters with typed literals, group-by, one aggregation, sort/rank and limit. No arbitrary formulas or joins initially. Add a join only if the implementation is ready to validate selected keys and cardinality without multiplication.

**Tests required:** correct totals/ranks/ties; missing/invalid columns; unsupported aggregation; empty/all-null data; null semantics; answerability outcomes for missing fields and ambiguity; malformed/oversized plans; malicious expression-shaped values are rejected as data/invalid operations; executor/verifier agree on fixtures; deliberate injected mismatch fails verification; tolerance/NaN behavior documented and tested.

**Acceptance criteria:** every operation comes from an explicit allow-list; rejected or unclear plans do not execute; numerical values are computed in Python; verifier is a distinct implementation; only completed successful checks can result in `VERIFIED`; an unanswerable question yields `CANNOT_DETERMINE` with reasons, never fabricated values.

### Milestone 3 — Evidence, provenance, proof graph and deterministic Python renderer

**Purpose:** make every result auditable and reproducible before connecting external services.

**Expected files:**
- `core/evidence.py`
- `core/proof_graph.py`
- `core/code_renderer.py` (fixed templates from validated plan; no `exec`)
- `tests/test_evidence.py`, `tests/test_proof_graph.py`, `tests/test_code_renderer.py`

**Dependencies:** Milestones 1–2; standard library/Pandas; Pytest.

**Tests required:** source fingerprint/table/column linkage; row counts/lineage through filters and aggregation; graph edges match the plan/evidence; serialization is deterministic and bounded; rendered Python contains only fixed constructs and correctly represents tested plans; hostile literals are escaped/parameterized; evidence reflects verifier status and limitations.

**Acceptance criteria:** one immutable evidence record is the source for result, code, status and graph; every result traces to a dataset version, columns, operations and verifier checks; renderer output is not executed by the app and is explicitly identified as generated reproducible code.

### Milestone 4 — Orchestration and LLM adapter (optional/configured)

**Purpose:** add natural-language convenience without making the model authoritative or making a model key necessary for core correctness.

**Expected files:**
- `core/agent.py` (orchestration only; deterministic stage ordering)
- `core/llm_client.py` (provider adapter and strict structured-output boundary)
- `tests/test_agent.py`, `tests/test_llm_client.py` (mock provider; no live API required in normal CI)

**Dependencies:** Milestones 1–3; selected provider SDK only if approved; secret supplied from environment or Streamlit secrets, never source. Provider choice, structured-output support, and privacy policy must be decided before implementation.

**Tests required:** valid structured plan; malformed/extra fields; nonexistent identifiers; prompt injection in question/cell text; timeouts/rate errors; missing key; no full-table transmission; bounded payload; clarification and `CANNOT_DETERMINE`; ensure no state mutation/tool invocation; ensure model cannot change execution result or verification status. Integration smoke test may be separately gated by an explicit API key.

**Acceptance criteria:** provider response is only a proposal; it must pass normal deterministic validation; no configured key still allows tests/demo using a fixed deterministic planner or clear unavailable state; logs/evidence do not expose credentials or raw sensitive rows.

### Milestone 5 — Minimal Streamlit demo

**Purpose:** connect the proven core to a clear end-to-end user flow.

**Expected files:**
- `app.py`
- `.streamlit/config.toml` (only non-secret settings; adjust `.gitignore` selectively if needed)
- `tests/test_app_smoke.py` or a documented manual smoke checklist if Streamlit test tooling adds disproportionate setup

**Dependencies:** Milestones 1–4; Streamlit; optional Plotly only if chart display is included. Test the full core without Streamlit/network wherever possible.

**Tests required:** upload and dataset replacement/session isolation; valid answer path; cannot-determine/clarification path; failure and timeout messages; evidence/code/proof graph shown; correct verification badge and limitations; no stale answer after a new file/question; chart values exactly match result table.

**Acceptance criteria:** a user can upload fixtures, ask a supported question, and see answer, plan-derived Python, evidence/provenance, proof graph and honest verification state; can ask an unsupported question and get a clear refusal; all states are bound to the same run and dataset fingerprint.

### Milestone 6 — Demo hardening and acceptance pass

**Purpose:** make the demo predictable on the hackathon machine and eliminate false proof claims.

**Expected files:**
- Additional `tests/test_end_to_end.py`, regression tests, and/or `tests/test_limits.py`
- Updates to `README.md`, `requirements.txt` (only if dependencies actually used), and `.gitignore` if justified
- Optional `core/charts.py` if charts are kept in scope

**Dependencies:** completed Milestone 5; target Windows/Python 3.12 and actual hackathon runtime; no container/service infrastructure for the MVP.

**Tests required:** full offline end-to-end suite; CSV/XLSX fixtures; bad/large file and plan limits; repeated-run determinism; verification mismatch regression; secret scan/review; manual clean-environment startup; check app behavior when LLM provider unavailable.

**Acceptance criteria:** all tests pass in a clean Python 3.12 environment; documented one-command launch; no API key in repository; no arbitrary Python execution; demo covers supported answer, missing-data refusal, provenance and verification; known limitations are visible and rehearsed.

## Dependencies and policy

- Keep initial runtime small: Pandas, OpenPyXL, Streamlit; Pytest for development/test. Add Plotly only if a useful chart is implemented. Add one provider SDK only after selecting a provider and validating structured output. Avoid agent orchestration frameworks, databases, queues, containers, and deployment platforms for this single-process hackathon demo.
- The inspected `.venv` has only `pip`; dependencies are not installed. This planning task did not install anything. At implementation approval, create/update a minimal dependency manifest and install into the workspace environment, then record versions for repeatability.
- Never commit `.env`, API secrets, uploaded datasets, or generated private evidence. API credentials belong in environment variables or Streamlit secrets and are server-side only.
- Keep samples synthetic and small. Do not rely on external network/API access for deterministic tests or baseline demo.

## Recommended implementation sequence and gates

1. Implement Milestone 1 and get parser/profile behavior and limits tested.
2. Implement Milestone 2 and prove refusal, deterministic calculations, and verifier failure cases before adding any model. This is the critical correctness gate.
3. Implement Milestone 3; ensure all evidence is derived from the same immutable run.
4. Implement Milestone 4 with a mock adapter first; connect a real provider only after privacy, structured schema, and secret handling are agreed.
5. Implement Milestone 5 against fixtures and an offline planner path; introduce the UI only after core contracts stabilize.
6. Implement Milestone 6, run the complete suite, and rehearse the exact demo path. Defer any feature that threatens the verification/security gate.

Do not begin the next milestone until the current acceptance criteria pass. Keep each milestone small enough to review and commit independently. Any requirement to execute arbitrary model-generated Python is a scope/security decision requiring a separate approved design; do not solve it by quietly adding `exec` or claiming a subprocess is a sandbox.

## First implementation milestone

After explicit approval to leave planning, begin **Milestone 1: contracts, repository foundation, and ingestion/profile**. First choose the validation-model approach (standard-library dataclasses plus boundary validators is the recommended minimal choice), then create only that milestone's listed files and synthetic tests. Do not start the Streamlit UI or LLM integration first.
