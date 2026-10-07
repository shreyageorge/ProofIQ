# ProofIQ

## Clone and run the Django website

The active UI is Django (not Streamlit). Install Python 3.12 and Ollama first,
then run the following in a PowerShell terminal:

```powershell
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd ProofIQ
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env.local
Set-ExecutionPolicy -Scope Process Bypass
.\run_proofiq.ps1
```

Open `http://127.0.0.1:8000`. Upload `sample_sales.csv`, then ask a question.
ProofIQ uses a locally running Ollama model by default. No ChatGPT/OpenAI login,
API key, or paid API credit is required, and uploaded data stays on this computer.

## Local AI setup (Ollama)

The default model is `qwen3.5:4b`. With Ollama installed, run:

```powershell
ollama serve
ollama pull qwen3.5:4b
```

The launcher starts Ollama, downloads the model on the first run when necessary,
and starts Django. Configuration lives in `.env.local`; copy the safe defaults
from `.env.example`. OpenAI remains optional and is used only when
`AI_PROVIDER=openai` is deliberately configured.

The Ollama runtime and model are intentionally not committed: they are several
gigabytes and are machine-specific. Each teammate installs Ollama once and the
launcher pulls the model into that teammate's local Ollama storage.

ProofIQ is a Proof-Carrying Data Analyst. This repository currently contains
the deterministic Milestone 1 data foundation, Milestone 2 analysis pipeline,
Milestone 3 evidence/provenance layer, and Milestone 4 orchestration boundary:
typed immutable contracts, bounded CSV/XLSX ingestion, reproducible profiling,
typed plan validation, trusted execution, and independent verification.

## Requirements

- Python 3.12
- Install dependencies with `python -m pip install -r requirements.txt`

Pandas parses CSV uploads in bounded chunks; OpenPyXL reads XLSX uploads in
read-only mode. Django stores uploads in the ignored `media/` directory for the
current session, and the per-file remove button or Clear workspace deletes them.

## Run tests

```text
pytest
```

The tests use synthetic local fixtures and do not require network access, an API
key, or a live LLM provider. Django is required for the presentation-layer tests.

## Supported uploads and limits

- CSV encoded as UTF-8 or UTF-8 with a byte-order mark.
- XLSX workbooks, with each selected worksheet treated as a logical table.
- Blank headers become `unnamed_column`; repeated names receive deterministic
  `__2`, `__3`, ... suffixes without overwriting source columns.
- Default limits: 25,000,000 uploaded bytes; 100,000 data rows per table;
  500 columns; 50 workbook sheets; 2,000,000 cells per table; and
  100,000,000 uncompressed XLSX archive bytes.
- Empty CSV cells and blank XLSX cells are represented as `None`. CSV values
  remain strings; XLSX cells retain their observed scalar types. Ambiguous
  date strings are not parsed or classified as date/time.
- XLSX formulas are loaded as formula text, never calculated. Macro-enabled
  formats are unsupported.
- Blank rows are omitted; source row numbers are retained for row IDs.
- Header-only and otherwise data-empty tables are rejected.
- Duplicate row count counts each repeated occurrence after the first.
- Column samples contain up to five distinct non-null values by default.
  Identifier, numeric, date/time, boolean, and categorical labels are
  deterministic hints, not verified business semantics.

All limits are configurable through `IngestionLimits`. Filenames are reduced
to a display-only basename and are never used to open or save a path.

## Deterministic analysis core

Milestone 2 adds typed `AnalysisPlan` steps and a validator for one table,
filters, one optional group/aggregation, stable sorting, bounded limits, and
projection. Use `QuestionIntent` for exact case-insensitive catalog field
resolution; similar or derived column names are never guessed. Ambiguous fields
require clarification, and answerability alone never authorizes execution.

CSV values remain strings in snapshots. Numeric comparisons require numeric
literals on columns conservatively profiled as numeric. Numeric aggregations
also reject identifier-like columns. Ambiguous/non-numeric columns are rejected
for numeric operations. `IS_NULL` is the only null filter; other comparisons do
not match nulls. `COUNT` without a column counts rows; column counts, `COUNT_DISTINCT`,
and numeric aggregates exclude nulls. `SUM` over only-null values returns null.
Sorts are stable and place nulls last in either direction; limits reject invalid
or oversized values rather than truncating them implicitly.

The executor interprets validated plans using Pandas and returns immutable
result rows with stable source-row lineage. The separate verifier recomputes
results from source tuples; its default numeric tolerance is `1e-9`. A
`VERIFIED` status covers only consistency with the provided dataset and plan,
not the truth of the source data or the user's intent.

Milestone 3 creates a bounded immutable evidence record, a deterministic typed
proof graph, and readable Pandas source rendered from a validated plan. The
rendered source is display-only and is never executed. Evidence records refer to
the uploaded snapshot by content fingerprint and summarize results by digest;
they do not persist or copy the uploaded dataset. No joins, SQL, LLM, or UI are
included in the Milestone 3 layer.

Milestone 4 adds a provider-neutral `LLMClient` protocol, strict structured
proposal parsing, an offline `MockLLMClient`, and an `Agent` that runs
answerability and plan validation before execution, independent verification,
evidence, and the proof graph. The LLM is only a plan proposer; the agent does
not ask it to calculate results or narrate answers. Catalog context contains
bounded schema/profile metadata only, not table rows or sample values. No real
provider SDK or API key is configured; callers must inject a client, and the
agent returns a structured unavailable state if no provider response is
available. Invalid proposals never reach the executor, and only `VERIFIED`
results are exposed as answer tables.

## Previous Streamlit application (replaced)

The Streamlit entry point has been replaced by Django. Use the run commands at
the top of this README. The deterministic ingestion, planning, execution,
verification, evidence, and proof-graph modules remain available under `core/`.

The former command was:

```text
python -m streamlit run app.py  # no longer used
```

The app keeps the current upload, profile, question, and outcome in the
individual Streamlit session. CSV/XLSX uploads are ingested in memory under the
core's configured limits; a small profile and bounded preview are shown without
displaying the full dataset. Reset clears the active workspace.

Answers are produced only by the existing `Agent` pipeline. Charts are optional
and are built only from a verified result table. Verification checks, the
evidence record, the proof graph, and generated Python are displayed from the
structured core outcome. Generated Python is for review only and is never
executed by the app. Uploads are not saved to disk.

Milestone 4 defines the provider boundary but does not configure a live LLM
provider. The app therefore uses `MockLLMClient` by default and clearly reports
when a plan proposal is unavailable; it does not guess a plan or fabricate an
answer. A provider can be injected through the existing `Agent`/`LLMClient`
boundary in a later configuration milestone. No API key is required for tests.
