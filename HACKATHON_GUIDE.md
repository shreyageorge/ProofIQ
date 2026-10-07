# ProofIQ hackathon guide

## Goal for qualification day

By 12:00 have one local flow that always works: upload `examples/sample_sales.csv`,
ask “What is the total sales?”, see the verified result, evidence, and reproducible
code; then ask “What is the profit?” and see `CANNOT_DETERMINE`. By 14:00 have a
public Vercel URL and a rehearsed fallback screen recording. After 14:00, freeze the
core flow and add only low-risk polish.

## Team roles

1. **Agent/LLM owner:** prompt, Ollama/hosted-provider adapter, typed-plan output,
   answerability tests, and the two-minute technical explanation.
2. **Data/proof owner:** ingestion, deterministic validator/executor/verifier,
   evidence/provenance, test data, edge cases, and numeric checking.
3. **Django/demo owner:** upload/session views, templates/CSS, Vercel, demo script,
   slides, screen recording, and timekeeping.

## 09:00–16:30 schedule

| Time | Agent/LLM owner | Data/proof owner | Django/demo owner | Gate |
|---|---|---|---|---|
| 09:00–09:20 | Verify model/provider and one structured-plan call. | Run all tests and record baseline. | Start Django and test upload UI. | Everyone can explain the architecture. |
| 09:20–10:00 | Test total, grouping, filtering, and missing-field questions. | Validate expected totals manually; add regression cases. | Fix only blocking UI/session errors. | Total sales = ₹564,800. |
| 10:00–11:00 | Tighten prompt/schema; no free-form code. | Test malformed CSV/XLSX, duplicates, nulls, limits. | Add status, evidence, proof chain, reset. | Happy path + refusal path work. |
| 11:00–11:30 | Prepare provider fallback explanation. | Run complete suite; freeze core after green. | Make a 90-second backup recording. | Release candidate 1. |
| 11:30–12:00 | Rehearse AI/safety questions. | Rehearse validation/verification questions. | Rehearse live demo and reset browser. | Round 1: functional website. |
| 12:00–12:30 | Fix only judge-observed blockers. | Retest changed paths. | Commit/push deployment candidate. | No feature creep. |
| 12:30–13:15 | Configure deploy-safe hosted model only if key exists. | Confirm secrets never enter logs/evidence. | Import repository into Vercel; set env vars. | Preview URL builds. |
| 13:15–13:45 | Smoke-test deployed planner or document limitation. | Check three canonical answers. | Test mobile/desktop, logs, 404/500 behavior. | Public URL or recorded fallback. |
| 13:45–14:00 | Help rehearse. | Save test output. | Promote tested build; capture URL/screenshots. | Round 2: deployment. |
| 14:00–15:00 | Optional: better clarification messages. | Optional: one quality badge/extra fixture. | Optional: chart from verified rows only. | Each change must take <20 min. |
| 15:00–15:45 | Prepare answers on LLM/API boundaries. | Prepare answers on hashes/provenance. | Prepare slides: problem, architecture, demo, impact. | Feature freeze at 15:45. |
| 15:45–16:15 | Run final questions. | Run tests and manual arithmetic. | Clean browser, preload sample, check URL/video. | Final rehearsal twice. |
| 16:15–16:30 | Present AI section. | Present proof/safety section. | Drive demo and closing. | Round 3: polished, stable story. |

Useful references: Django deployment checklist (`docs.djangoproject.com/en/5.2/howto/deployment/checklist/`), Vercel Django/backend docs (`vercel.com/docs/frameworks/backend`), Ollama structured output docs (`docs.ollama.com/capabilities/structured-outputs`), and Pandas documentation (`pandas.pydata.org/docs/`).

## Prompts each teammate can give an AI coding assistant

**Agent owner:** “Inspect the typed plan schema and provider boundary. Return the
smallest patch that makes the model emit schema-valid JSON only. Do not generate or
execute Python. Add tests for malformed, unsupported, missing-field, and timeout
responses, then run those tests.”

**Data owner:** “Trace one request from ingestion through answerability, validation,
execution, verification, evidence, and proof graph. Identify any point where an
untrusted value could bypass an allow-list. Patch only demonstrated gaps and add a
regression test for each.”

**Django owner:** “Test this Django flow as a new user: upload, ask, reset, replace
dataset, malformed upload, mobile width. Fix only reproducible failures; preserve
answerability and verification labels. Run Django checks and the full tests.”

## API and LLM essentials

An API is a contract that lets one program request a service from another. An HTTP
API request has a URL, method (`GET` reads, `POST` submits), headers (metadata and
authentication), and an optional body (usually JSON). The response contains a
status code and body. `2xx` means success, `4xx` means the request/auth is wrong,
and `5xx` means the service failed. Integration means building the request, keeping
credentials server-side, validating the response, handling timeouts/retries, and
turning provider failures into safe product behavior.

In ProofIQ the browser posts a question to Django. Django sends only bounded schema
metadata—not raw cell samples—to the LLM. The LLM proposes typed JSON operations.
ProofIQ treats that response as untrusted, parses it strictly, checks table/column
IDs and allowed operations, assesses whether the data can answer the question,
executes trusted internal Pandas operations, and independently recomputes/checks
the result. Finally it builds evidence and display-only Python from the validated
plan. The LLM is a planner, not the calculator or verifier.

Ollama exposes a local HTTP API on `127.0.0.1:11434`; this is free and private but
exists only on the laptop. Vercel runs the Django request on a remote function, so
its `127.0.0.1` refers to Vercel—not your laptop. A public deployment needs a hosted
model API or a separately hosted Ollama endpoint. API keys belong in environment
variables and must never be committed, rendered, logged, or pasted into prompts.

## Files to know

- `manage.py`: Django command entry point.
- `dataguard/settings.py`: apps, middleware, templates, uploads, providers, and production settings.
- `dataguard/urls.py`: routes requests into the analyst app.
- `dataguard/wsgi.py` and `app.py`: WSGI entry points for local/hosting runtimes.
- `analyst/views.py`: upload, question, reset, and presentation orchestration.
- `analyst/services.py`: current provider/data bridge; this must not bypass `core/` in the final secure architecture.
- `core/ingestion.py`: safe CSV/XLSX parsing and limits.
- `core/llm_client.py`: schema-only context and strict untrusted-plan parsing.
- `core/answerability.py`: decides answerable/partial/clarification/cannot-determine.
- `core/planner.py`: deterministic plan allow-list and column validation.
- `core/executor.py`: trusted typed operations; it does not execute generated Python.
- `core/verifier.py`: independent result checks.
- `core/evidence.py`, `proof_graph.py`, `code_renderer.py`: audit trail, visual chain, and display-only code.
- `templates/analyst/dashboard.html`: server-rendered UI.
- `static/`: CSS and small browser behavior.
- `tests/`: executable proof of expected behavior.
- `vercel.json`, `.python-version`: deployment metadata.

The most important honest answer to an invigilator is: “The model can be wrong, so
we do not trust its prose or code. It may only propose a small typed plan, and three
deterministic boundaries—answerability, validation, and verification—must agree
before the UI labels an answer verified.”
