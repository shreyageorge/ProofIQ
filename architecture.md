# ProofIQ — System Architecture

## Product

ProofIQ is a Proof-Carrying Data Analyst.

The system allows a user to upload messy multi-table CSV/XLSX data, ask analytical questions in natural language, and receive an answer together with executable Python code, evidence, provenance, and verification status.

The core principle is:

> Every numerical answer should carry enough evidence and executable logic to reproduce and verify it.

---

# Core Requirements

## 1. Multi-table data

Support:

- CSV
- XLSX
- multiple files
- multiple sheets where practical

The system must inspect uploaded data before attempting analysis.

---

## 2. Data understanding

The system should identify:

- table names
- column names
- data types
- missing values
- duplicate rows
- unique values
- likely identifiers
- categorical columns
- numerical measures
- date/time columns
- potential relationships between tables

---

## 3. Question understanding

The user asks a natural-language analytical question.

Example:

"Which region had the highest revenue in Q3?"

The system must determine:

- required tables
- required columns
- filters
- grouping
- aggregation
- comparison
- ranking
- time period
- derived metrics

---

## 4. Answerability gate

Before generating an answer, the system must determine whether the available data is sufficient.

Possible outcomes:

ANSWERABLE

PARTIALLY_ANSWERABLE

CANNOT_DETERMINE

The system must never invent unavailable information.

---

## 5. Analysis planning

For an answerable question, generate a structured analysis plan.

Example:

{
  "tables": ["sales"],
  "filters": ["quarter == 'Q3'"],
  "group_by": ["region"],
  "metric": "sum(amount)",
  "sort": "descending",
  "required_columns": [
    "region",
    "quarter",
    "amount"
  ]
}

The exact implementation may use Pydantic models or another typed representation.

---

## 6. Executable analysis

The system generates Python/Pandas code based on the approved analysis plan.

The code must be:

- readable
- deterministic where possible
- reproducible
- limited to approved data
- suitable for execution in a controlled environment

---

## 7. Execution

Execute the generated analysis code in a controlled execution layer.

Capture:

- stdout
- errors
- result
- execution time
- generated artifacts

If execution fails, the agent may attempt a limited repair cycle.

---

## 8. Verification

The system must verify important numerical results independently.

Verification may include:

- independent recomputation
- aggregation consistency
- row-count checks
- missing-data checks
- duplicate checks
- source-column validation
- assumption validation
- comparison of generated result with independently calculated result

Possible verification states:

VERIFIED

PARTIALLY_VERIFIED

FAILED

---

## 9. Evidence / provenance

Every answer should maintain an evidence record containing:

- original question
- datasets used
- columns used
- filters applied
- transformations
- calculations
- result
- verification status
- limitations
- generated code

---

## 10. Proof graph

Represent the reasoning/evidence chain as:

QUESTION
    ↓
DATASET
    ↓
COLUMNS
    ↓
FILTER
    ↓
TRANSFORMATION
    ↓
CALCULATION
    ↓
RESULT
    ↓
VERIFICATION

The UI should eventually visualize this chain.

---

# Major Components

## UI Layer

Responsible for:

- upload interface
- dataset workspace
- question input
- loading states
- result display
- charts
- evidence
- proof graph
- Python code viewer
- errors

Technology:

Streamlit

---

## Data Layer

Components:

- ingestion
- file validation
- dataframe management
- profiling
- schema inference
- semantic typing
- relationship detection
- data-quality analysis

Technology:

Python
Pandas
OpenPyXL

---

## Agent Layer

Components:

- question interpreter
- answerability gate
- query planner
- code generator
- error analyzer
- repair loop

The LLM should not directly control the application state.

It should produce structured outputs consumed by deterministic Python code.

---

## Execution Layer

Responsible for:

- executing generated analysis
- capturing results
- handling errors
- enforcing timeouts
- preventing uncontrolled operations

Do not expose unrestricted filesystem or operating-system operations to generated analysis code.

---

## Verification Layer

Responsible for:

- independent recomputation
- consistency checks
- validation
- verification status

The verification layer must be as deterministic as possible and must not blindly trust the original LLM output.

---

## Evidence Layer

Responsible for:

- provenance
- evidence records
- proof graph
- reproducibility information

---

## Visualization Layer

Responsible for:

- appropriate charts
- tables
- proof visualization
- result summaries

Use Plotly where interactive charts are useful.

---

# Design Principles

1. Never hallucinate missing data.
2. Never present an unverified numerical result as verified.
3. Prefer deterministic Python calculations over LLM arithmetic.
4. Separate planning from execution.
5. Separate execution from verification.
6. Preserve provenance.
7. Make important calculations reproducible.
8. Keep the system modular.
9. Keep the MVP understandable and demoable.
10. Favor reliability over unnecessary complexity.

---

# Technology Stack

Python 3.12

Streamlit

Pandas

OpenPyXL

Plotly

Pytest

Git/GitHub

LLM API

---

# Initial Project Structure

ProofIQ/

├── app.py

├── core/
│   ├── __init__.py
│   ├── ingestion.py
│   ├── profiler.py
│   ├── schema.py
│   ├── planner.py
│   ├── agent.py
│   ├── executor.py
│   ├── verifier.py
│   ├── evidence.py
│   ├── proof_graph.py
│   └── charts.py

├── models/
│   ├── __init__.py
│   └── schemas.py

├── tests/
│   ├── test_ingestion.py
│   ├── test_profiler.py
│   ├── test_planner.py
│   ├── test_executor.py
│   ├── test_verifier.py
│   └── test_evidence.py

├── data/
│   └── sample/

├── .streamlit/
│   └── config.toml

├── architecture.md
├── requirements.txt
├── README.md
└── .gitignore

---

# Development Strategy

Build in verified increments.

Phase 1:
Repository and architecture.

Phase 2:
Data ingestion and profiling.

Phase 3:
Structured question planning.

Phase 4:
Answerability gate.

Phase 5:
Code generation.

Phase 6:
Controlled execution.

Phase 7:
Independent verification.

Phase 8:
Evidence and proof graph.

Phase 9:
Charts and UI.

Phase 10:
Deployment and testing.

Do not implement all phases at once.

After each phase:

- run tests
- run the application if applicable
- inspect generated code
- commit working changes

---

# Hackathon Goal

The final demonstration should show:

1. Upload multiple messy datasets.
2. Ask a natural-language analytical question.
3. ProofIQ identifies the required data.
4. ProofIQ determines whether the question is answerable.
5. ProofIQ generates executable analysis code.
6. ProofIQ executes the analysis.
7. ProofIQ independently verifies the result.
8. ProofIQ displays the answer.
9. ProofIQ displays evidence and provenance.
10. ProofIQ displays the reproducible Python proof.
11. ProofIQ correctly refuses questions that cannot be answered from the uploaded data.