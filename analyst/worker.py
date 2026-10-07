import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if pd.isna(value):
        return None
    return value


payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
dfs = {name: pd.read_json(path, orient="table") for name, path in payload["manifest"].items()}
scope = {"pd": pd, "np": np, "dfs": dfs, "result": None, "evidence": [], "quality_notes": []}
safe_builtins = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "range": range, "round": round,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "isinstance": isinstance,
}
exec(compile(payload["code"], "<generated-analysis>", "exec"), {"__builtins__": safe_builtins}, scope)
output = {"result": clean(scope["result"]), "evidence": clean(scope["evidence"]), "quality_notes": clean(scope["quality_notes"])}
Path(sys.argv[2]).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
