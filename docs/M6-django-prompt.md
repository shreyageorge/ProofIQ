# M6 prompt adapted for Django + Vercel

Use this prompt with a coding agent from the `ProofIQ` repository root:

> Implement Milestone 6—deployment, production hardening, and hackathon readiness
> for the existing ProofIQ Django application. Inspect the repository and run the
> complete tests before changing anything. Preserve the typed `core/` contracts:
> the LLM may propose structured plans only; it must never generate executable code.
> Validate every proposal deterministically, execute only allow-listed typed Pandas
> operations, independently verify the output, and derive evidence, proof graphs,
> and display-only reproducible Python from the validated plan.
>
> Harden Django uploads for CSV/XLSX with size, row, column, blank-header,
> duplicate-header, malformed-file, and workbook checks. Never show raw tracebacks.
> Keep every upload and analysis bound to the Django session, dataset fingerprint,
> question ID, plan ID, and run ID. Add a New Analysis action that deletes uploaded
> temporary files and clears stale result state.
>
> Prepare deployment for Vercel's Django runtime using `app.py`, `.python-version`,
> `requirements.txt`, and minimal `vercel.json`. Use environment variables for all
> secrets. On Vercel use `/tmp` only as explicitly temporary storage and document
> that durable/multi-instance uploads require Vercel Blob or equivalent. Do not
> claim that a localhost Ollama service works from Vercel Functions. Keep Ollama as
> the local provider and fail clearly when no deploy-safe hosted provider exists.
>
> Make the Django template show answerability and verification states accurately,
> an evidence panel, a proof-chain view made from actual graph nodes, and generated
> Python as evidence only. Charts may use only verified result rows. Never relabel a
> failed or partially verified result as verified.
>
> Update README and `docs/deployment.md`, add a safe sample CSV, review `.gitignore`,
> search for `eval`, `exec`, `subprocess`, `os.system`, and `shell=True`, and prove no
> user/LLM value reaches an execution primitive. Add deployment-critical tests and
> run `.venv\Scripts\python.exe -m pytest -q` plus a real Django smoke test with
> `.venv\Scripts\python.exe manage.py runserver`. Report original and final test
> counts, changed files, verified flows, deployment status, and remaining limits.

This version replaces every Streamlit-specific instruction with Django sessions,
views, templates, static assets, and Vercel's Django runtime. It also explicitly
preserves the safety architecture already present in `core/`.
