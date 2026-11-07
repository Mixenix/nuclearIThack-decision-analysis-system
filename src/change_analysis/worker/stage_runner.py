"""Entry point of the computing subprocess.

Started by the worker as a **separate interpreter** (this is also the only place
where cudf.pandas may be switched on, before pandas is imported — PLAN.md §18.1).
It receives no database credentials, no session secret and no LLM key.

    python -m change_analysis.worker.stage_runner <spec.json>
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path

from .context import ProcedureContext


def _load_module(code_path: Path):
    name = "user_procedure_" + hashlib.sha1(
        str(code_path).encode("utf-8") + str(time.time()).encode()
    ).hexdigest()[:10]
    spec = importlib.util.spec_from_file_location(name, code_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import procedure from {code_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    spec_path = Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    stage = spec["stage"]
    run_id = spec["run_id"]
    backend = spec.get("backend", "cpu")
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    code_path = Path(spec["code_path"])

    ctx = ProcedureContext(run_id=run_id, stage=stage, backend=backend, out_dir=out_dir)
    started = time.perf_counter()
    result = {"ok": False, "stage": stage, "counts": {}, "meta": {}, "error": "",
              "diagnostics": [], "duration_sec": 0.0}
    try:
        module = _load_module(code_path)
        procedure = getattr(module, "PROCEDURE", None)
        if not isinstance(procedure, dict) or "entrypoint" not in procedure:
            raise ValueError(
                "the procedure must export PROCEDURE = {'id','label','stage',"
                "'contract_version','params','entrypoint'}"
            )
        declared = procedure.get("stage")
        if declared != stage:
            raise ValueError(f"procedure declares stage {declared!r} but was run as {stage!r}")
        params = dict(procedure.get("params") or {})
        params.update(spec.get("params") or {})
        entry = procedure["entrypoint"]
        value = entry(ctx, spec.get("inputs") or {}, params)

        counts = dict(ctx.meta)
        if value is not None:
            for attr, table in (("rows", "rows"), ("segments", "segments"),
                                ("product_segments", "product_segments"),
                                ("facts", "facts")):
                payload = getattr(value, attr, None)
                if payload:
                    counts[table] = ctx.write_rows(table, payload)
            diagnostics = getattr(value, "diagnostics", None) or []
            result["diagnostics"] = [d.as_dict() if hasattr(d, "as_dict") else d
                                     for d in diagnostics]
        ctx.close()
        result["counts"] = {k: int(v) for k, v in counts.items() if isinstance(v, int)}
        result["meta"] = ctx.meta
        if stage == "normalize":
            errors = [d for d in result["diagnostics"]
                      if str(d.get("level", "")).upper() == "ERROR"]
            if errors:
                raise ValueError("normalization errors: " + "; ".join(
                    f"{d.get('code', '')} {d.get('where', '')}: {d.get('message', '')}"
                    for d in errors
                ))
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - the subprocess must always report
        ctx.close()
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-6000:]
    finally:
        result["duration_sec"] = round(time.perf_counter() - started, 3)
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            ctx.close()
        except Exception:  # pragma: no cover
            pass
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
