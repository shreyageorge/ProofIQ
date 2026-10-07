import ast
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
from django.conf import settings
from openai import APIConnectionError, APIStatusError, OpenAI


ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xls"}
MAX_ROWS = 200_000


def safe_name(name: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(name).stem).strip("_") or "dataset"
    return stem[:60]


def load_tables(file_records):
    tables = {}
    sources = {}
    for record in file_records:
        path = Path(record["path"])
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path, nrows=MAX_ROWS)
            key = safe_name(record["name"])
            tables[key] = frame
            sources[key] = record["name"]
        else:
            with pd.ExcelFile(path) as workbook:
                for sheet in workbook.sheet_names:
                    key = f"{safe_name(record['name'])}__{safe_name(sheet)}"
                    tables[key] = pd.read_excel(
                        workbook,
                        sheet_name=sheet,
                        nrows=MAX_ROWS,
                    )
                    sources[key] = f"{record['name']} / {sheet}"
    return tables, sources


def _clean(value):
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def profile_tables(tables, sources):
    profile = {}
    for key, frame in tables.items():
        sample = [{str(k): _clean(v) for k, v in row.items()} for row in frame.head(3).to_dict("records")]
        profile[key] = {
            "source": sources[key],
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "dtypes": {str(c): str(frame[c].dtype) for c in frame.columns},
            "missing": {str(c): int(frame[c].isna().sum()) for c in frame.columns},
            "duplicate_rows": int(frame.duplicated().sum()),
            "sample": sample,
        }
    return profile


def is_missing_data_question(question):
    words = set(re.findall(r"[a-z]+", question.casefold()))
    return (
        "missing" in words and bool(words.intersection({"data", "value", "values", "column", "columns"}))
    ) or bool(words.intersection({"nulls", "null", "missingness", "completeness"}))


def summarize_missing_data(profile):
    """Build a deterministic missing-value report from the existing table profiles."""
    evidence = []
    result_lines = []
    for table_name, table in profile.items():
        missing = table.get("missing", {})
        columns = table.get("columns", [])
        counts = [(column, int(missing.get(column, 0))) for column in columns]
        evidence.extend(
            {"table": table_name, "column": column, "missing_count": count}
            for column, count in counts
        )
        counts_text = ", ".join(f"{column}: {count}" for column, count in counts)
        result_lines.append(
            f"{table_name} ({table.get('rows', 0)} rows): {counts_text or 'no columns'}"
        )
    return {
        "result": "\n".join(result_lines) or "No tables are available to profile.",
        "evidence": evidence,
        "quality_notes": [],
    }


def create_deterministic_plan(question, profile):
    """Plan common audit-friendly aggregations without depending on model output."""
    query = question.casefold()
    aggregate = None
    if any(word in query for word in ("total", "sum")):
        aggregate = "sum"
    elif any(word in query for word in ("average", "mean")):
        aggregate = "mean"
    elif any(word in query for word in ("highest", "top", "maximum", "max")):
        aggregate = "sum"
    if aggregate is None:
        return None

    table_name = metric = None
    metric_aliases = {
        "sales": ("sales", "revenue", "amount"),
        "rating": ("rating",),
        "quantity": ("quantity", "units"),
        "price": ("price",),
    }
    for candidate_table, details in profile.items():
        for column in details.get("columns", []):
            normalized = str(column).casefold().replace("_", " ")
            if any(
                alias in query and (kind in normalized or alias in normalized)
                for kind, aliases in metric_aliases.items()
                for alias in aliases
            ):
                table_name, metric = candidate_table, str(column)
                break
        if metric:
            break
    if not metric:
        return None

    group_column = None
    for column in profile[table_name].get("columns", []):
        normalized = str(column).casefold().replace("_", " ")
        if re.search(rf"\bby\s+(?:each\s+)?{re.escape(normalized)}\b", query):
            group_column = str(column)
            break
        if any(word in query for word in ("highest", "top")) and normalized in query:
            group_column = str(column)
            break

    table_literal, metric_literal = repr(table_name), repr(metric)
    if group_column:
        group_literal = repr(group_column)
        code = (
            f"df = dfs[{table_literal}]\n"
            f"working = df[[{group_literal}, {metric_literal}]].copy()\n"
            f"working[{metric_literal}] = pd.to_numeric(working[{metric_literal}], errors='coerce')\n"
            f"grouped = working.dropna(subset=[{group_literal}, {metric_literal}]).groupby({group_literal}, as_index=False)[{metric_literal}].{aggregate}()\n"
            f"grouped = grouped.sort_values({metric_literal}, ascending=False)\n"
        )
        if any(word in query for word in ("highest", "top", "maximum", "max")):
            code += (
                "winner = grouped.iloc[0]\n"
                f"result = {{{group_literal}: winner[{group_literal}], {metric_literal}: float(winner[{metric_literal}])}}\n"
            )
        else:
            code += (
                "result = {str(row[" + group_literal + "]): float(row[" +
                metric_literal + "]) for _, row in grouped.iterrows()}\n"
            )
        code += "evidence = grouped.head(20).to_dict('records')\nquality_notes = []"
    else:
        code = (
            f"df = dfs[{table_literal}]\n"
            f"values = pd.to_numeric(df[{metric_literal}], errors='coerce')\n"
            f"result = float(values.{aggregate}())\n"
            f"evidence = [{{'table': {table_literal}, 'metric': {metric_literal}, 'operation': '{aggregate}', 'value': result, 'rows_used': int(values.notna().sum())}}]\n"
            "quality_notes = []"
        )
    return {
        "status": "READY",
        "reason": "The requested aggregation is supported directly by the uploaded columns.",
        "code": code,
        "assumptions": [],
        "used_tables": [table_name],
        "provider_label": "ProofIQ deterministic planner (verified pandas)",
    }


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["READY", "CANNOT_DETERMINE"]},
        "reason": {"type": "string"},
        "code": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "used_tables": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "reason", "code", "assumptions", "used_tables"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are ProofIQ's analysis planner. You receive a user's question and factual profiles of pandas DataFrames already loaded in a dictionary named dfs.

Each uploaded file is authoritative for the user's question. Each profile's `rows` value is the complete DataFrame row count, and every one of those rows is loaded in `dfs`. Its `sample` field is merely a three-row schema preview. Never infer that a dataset is incomplete because the filename/table name contains the word "sample", because only three preview records are shown, or because the table is small. A total, average, count, filter, or grouping is READY whenever its required columns exist. The user is asking about the uploaded data, not an unknown larger real-world dataset.

Decide whether the requested answer is supported by the available columns and rows. If essential data is missing, return CANNOT_DETERMINE and empty code. Never invent columns, joins, units, dates, or business definitions.

If supported, return short, rerunnable Python/pandas code. The code runs with pd, np, and dfs already defined. It must:
1. use only provided table keys and exact column names;
2. perform joins explicitly when needed;
3. assign a JSON-serializable final value to `result`;
4. assign a list of evidence records to `evidence` (max 20 rows);
5. assign a list of data-quality notes to `quality_notes`;
6. not import, open files, access the network/environment, use eval/exec/compile, or catch broad exceptions.

Prefer transparent pandas operations. Convert dates with pd.to_datetime(..., errors='coerce'). Use numeric coercion when appropriate. Keep code under 40 lines. Do not format numbers into unsupported currency units.

For ranking questions such as "which product has the highest sales", assign a
dictionary containing both the winning group label and its aggregated value to
result. Sort the grouped evidence by the requested metric before selecting the
winner. `quality_notes` is only for actual warnings; leave it empty when values
are valid.

For scalar questions such as total, average, minimum, or maximum, assign the
scalar aggregation directly to result. Never index an aggregation result with
[0] or another label: Series indexing uses labels and commonly raises KeyError.
For example, use result = float(df["Amount"].sum()), not
result = df["Amount"].sum()[0]."""

# A concrete syntax example keeps small local models from producing prose or
# malformed comprehensions. Table and column names below are illustrative only.
SYSTEM_PROMPT += """

Code syntax example (replace names and operations to match the actual profile):
df = dfs["table_key"]
result = float(df["Amount"].sum())
evidence = [{"metric": "sum", "value": result, "rows_used": int(len(df))}]
quality_notes = []

Never write imports. Never use a bare `for` after an assignment. `evidence` must be
a normal list of dictionaries, not a generator or malformed comprehension.
"""


def _create_openai_plan(question, profile, execution_error=None):
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    planner_input = {"question": question, "data_profile": profile}
    if execution_error:
        planner_input["previous_execution_error"] = execution_error
    try:
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=json.dumps(planner_input, ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": "analysis_plan", "strict": True, "schema": ANALYSIS_SCHEMA}},
        )
    except APIConnectionError as exc:
        raise RuntimeError("The AI service could not be reached. Check the server's internet connection and retry.") from exc
    except APIStatusError as exc:
        api_code = getattr(exc, "code", "") or ""
        body = getattr(exc, "body", {}) or {}
        nested_code = body.get("code", "") if isinstance(body, dict) else ""
        if "billing" in f"{api_code} {nested_code} {exc}".lower() or nested_code == "insufficient_quota":
            raise RuntimeError("OpenAI API billing is not active for this project. Enable billing, then retry—no hard-coded answer was substituted.") from exc
        raise RuntimeError(f"The AI service rejected this run ({exc.status_code}). Check model/project access and retry.") from exc
    plan = json.loads(response.output_text)
    plan["provider_label"] = f"OpenAI · {settings.OPENAI_MODEL}"
    return plan


def _ollama_request(path, payload=None, timeout=180):
    base = settings.OLLAMA_URL
    parsed = urllib.parse.urlparse(base)
    is_local = base in {"http://127.0.0.1:11434", "http://localhost:11434"}
    is_cloud = parsed.scheme == "https" and parsed.netloc == "ollama.com" and not parsed.path
    if not (is_local or is_cloud):
        raise RuntimeError(
            "OLLAMA_URL must be http://127.0.0.1:11434 for local use or "
            "https://ollama.com for a deployed app."
        )
    headers = {"Content-Type": "application/json"}
    if is_cloud:
        if not settings.OLLAMA_API_KEY:
            raise RuntimeError(
                "Ollama Cloud is selected, but OLLAMA_API_KEY is missing. Add it "
                "to the Vercel project environment variables and redeploy."
            )
        headers["Authorization"] = f"Bearer {settings.OLLAMA_API_KEY}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", data=data,
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if is_cloud and exc.code in {401, 403}:
            raise RuntimeError(
                "Ollama Cloud rejected the API key. Replace OLLAMA_API_KEY in "
                "Vercel and redeploy."
            ) from exc
        raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        if is_cloud:
            raise RuntimeError(
                "Ollama Cloud could not be reached from the deployment. Please retry."
            ) from exc
        raise RuntimeError(
            "The local Ollama service is not reachable. Start it with `ollama serve`, then retry."
        ) from exc


def ollama_status():
    if settings.OLLAMA_URL == "https://ollama.com":
        ready = bool(settings.OLLAMA_API_KEY)
        return {"online": ready, "ready": ready, "model": settings.OLLAMA_MODEL}
    try:
        tags = _ollama_request("/api/tags", timeout=2)
        installed = {item.get("name", "") for item in tags.get("models", [])}
        model = settings.OLLAMA_MODEL
        ready = model in installed or any(name.split(":")[0] == model.split(":")[0] for name in installed)
        return {"online": True, "ready": ready, "model": model}
    except RuntimeError:
        return {"online": False, "ready": False, "model": settings.OLLAMA_MODEL}


def _create_ollama_plan(question, profile, execution_error=None):
    planner_input = {"question": question, "data_profile": profile}
    if execution_error:
        planner_input["previous_execution_error"] = execution_error
    response = _ollama_request("/api/chat", {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(planner_input, ensure_ascii=False)},
        ],
        "stream": False,
        "think": False,
        "format": ANALYSIS_SCHEMA,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 900},
    })
    message = response.get("message", {}) if isinstance(response, dict) else {}
    candidates = [
        message.get("content"),
        message.get("thinking"),
        message.get("tool_calls"),
        response.get("response") if isinstance(response, dict) else None,
    ]

    def extract_plan(value, depth=0):
        """Accept the response variants used by local and hosted Ollama models."""
        if depth > 5 or value is None:
            return None
        if isinstance(value, dict):
            if {"status", "reason", "code"}.issubset(value):
                return value
            for key in ("arguments", "content", "text", "thinking", "function"):
                found = extract_plan(value.get(key), depth + 1)
                if found:
                    return found
            return None
        if isinstance(value, list):
            for item in value:
                found = extract_plan(item, depth + 1)
                if found:
                    return found
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.IGNORECASE
        )
        for parser in (json.loads, ast.literal_eval):
            try:
                decoded = parser(cleaned)
            except (json.JSONDecodeError, ValueError, SyntaxError):
                continue
            found = extract_plan(decoded, depth + 1)
            if found:
                return found
        start = cleaned.find("{")
        if start >= 0:
            try:
                decoded, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                return extract_plan(decoded, depth + 1)
            except json.JSONDecodeError:
                pass
        return None

    plan = next((found for item in candidates if (found := extract_plan(item))), None)
    if not isinstance(plan, dict):
        location = "cloud" if settings.OLLAMA_URL == "https://ollama.com" else "local"
        raise RuntimeError(f"Ollama {location} returned an invalid analysis plan. Please retry.")
    # The model occasionally repeats an import even though pandas is already
    # provided by the isolated worker. Imports are never needed or permitted.
    if plan.get("status") == "READY" and isinstance(plan.get("code"), str):
        plan["code"] = "\n".join(
            line for line in plan["code"].splitlines()
            if not line.strip().startswith(("import ", "from "))
        ).strip()
    location = "cloud" if settings.OLLAMA_URL == "https://ollama.com" else "local"
    plan["provider_label"] = f"Ollama / {settings.OLLAMA_MODEL} ({location})"
    return plan


def create_plan(question, profile, *, execution_error=None):
    normalized_columns = {
        str(column).strip().casefold().replace("_", " ")
        for table in profile.values()
        for column in table.get("columns", [])
    }
    question_words = set(re.findall(r"[a-z]+", question.casefold()))
    if question_words.intersection({"profit", "profits", "margin", "margins"}):
        has_profit = any("profit" in column or "margin" in column for column in normalized_columns)
        has_cost = any("cost" in column or "expense" in column for column in normalized_columns)
        if not has_profit and not has_cost:
            return {
                "status": "CANNOT_DETERMINE",
                "reason": "Profit requires a profit, margin, cost, or expense field. The uploaded data contains sales revenue but no cost/profit data, so ProofIQ will not invent it.",
                "code": "",
                "assumptions": [],
                "used_tables": [],
                "provider_label": (
                    f"Ollama / {settings.OLLAMA_MODEL} "
                    f"({'cloud' if settings.OLLAMA_URL == 'https://ollama.com' else 'local'}) "
                    "+ deterministic answerability guard"
                ),
            }
    deterministic = create_deterministic_plan(question, profile)
    if deterministic:
        return deterministic
    provider = settings.AI_PROVIDER
    if provider == "openai":
        return _create_openai_plan(question, profile, execution_error)
    if provider != "ollama":
        raise RuntimeError(f"Unsupported AI_PROVIDER: {provider}")
    try:
        return _create_ollama_plan(question, profile, execution_error)
    except RuntimeError:
        if settings.AI_FALLBACK_PROVIDER == "openai":
            return _create_openai_plan(question, profile, execution_error)
        raise


def is_invalid_completeness_refusal(plan):
    """Detect a model refusal that contradicts the complete-upload contract."""
    if plan.get("status") != "CANNOT_DETERMINE":
        return False
    reason = str(plan.get("reason", "")).casefold()
    return any(
        phrase in reason
        for phrase in (
            "sample",
            "small subset",
            "only 12 rows",
            "insufficient rows",
            "larger dataset",
            "entire dataset",
            "full dataset",
            "not represent the full",
        )
    )


BLOCKED_NODES = (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Lambda, ast.ClassDef, ast.FunctionDef,
                 ast.AsyncFunctionDef, ast.Global, ast.Nonlocal, ast.Delete, ast.Raise, ast.Try)
BLOCKED_NAMES = {"open", "eval", "exec", "compile", "input", "help", "globals", "locals", "vars", "dir",
                 "getattr", "setattr", "delattr", "__import__", "os", "sys", "subprocess", "pathlib", "socket"}


def validate_code(code):
    if len(code) > 8_000:
        raise ValueError("Generated analysis is too long.")
    tree = ast.parse(code, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, BLOCKED_NODES):
            raise ValueError(f"Blocked Python construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise ValueError(f"Blocked name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Private/dunder attribute access is blocked.")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BLOCKED_NAMES:
            raise ValueError(f"Blocked call: {node.func.id}")
    assigned = {node.targets[0].id for node in ast.walk(tree) if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    missing = {"result", "evidence", "quality_notes"} - assigned
    if missing:
        raise ValueError(f"Generated code did not assign: {', '.join(sorted(missing))}")


def execute_plan(code, tables):
    validate_code(code)
    with tempfile.TemporaryDirectory(prefix="dataguard_") as temp_dir:
        temp = Path(temp_dir)
        manifest = {}
        for key, frame in tables.items():
            path = temp / f"{safe_name(key)}.json"
            frame.to_json(path, orient="table", date_format="iso")
            manifest[key] = str(path)
        payload_path = temp / "payload.json"
        output_path = temp / "output.json"
        payload_path.write_text(json.dumps({"manifest": manifest, "code": code}), encoding="utf-8")
        worker = Path(__file__).with_name("worker.py")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(
            [sys.executable, "-I", str(worker), str(payload_path), str(output_path)],
            capture_output=True, text=True, timeout=12, cwd=temp, env=env,
        )
        if completed.returncode != 0:
            output = completed.stderr or completed.stdout or ""
            error_match = re.search(
                r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::|\b)",
                output,
            )
            error_type = error_match.group(1) if error_match else "execution error"
            raise RuntimeError(f"Generated pandas analysis failed ({error_type}).")
        return json.loads(output_path.read_text(encoding="utf-8"))


def verify_result(execution):
    checks = [
        {"label": "Generated code passed safety checks", "ok": True},
        {"label": "Code executed successfully", "ok": True},
        {"label": "Result is JSON-serializable", "ok": True},
    ]
    notes = execution.get("quality_notes") or []
    checks.append({"label": "No reported data-quality warning", "ok": not notes})
    return checks
