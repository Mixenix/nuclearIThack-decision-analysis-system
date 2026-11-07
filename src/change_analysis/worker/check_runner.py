"""Runs one check in an isolated interpreter under a read-only database role.

    python -m change_analysis.worker.check_runner <spec.json>

The spec carries the check's source text, its params and the run id.  The Mart
object is the only thing the check code can reach; the connection string is used
by the runner, never handed to the user code.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def _run_app_ids(run_id: str) -> set[str] | None:
    """Applications of this run, so APP_IDS cannot point at somebody else's run.

    Returns ``None`` when the list cannot be read: a broken reference is then
    reported by the service layer rather than blocking the check itself.
    """
    try:
        import os

        import psycopg

        # the runner hands the subprocess a read-only DSN (role ca_ro)
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            return None
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT app_id FROM ca.app_segments WHERE run_id = %s",
                            (run_id,))
                return {row[0] for row in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "rows": 0, "error": "", "traceback": "", "duration_ms": 0,
              "check_id": spec.get("check_id", "")}
    started = time.perf_counter()
    try:
        import importlib.util

        from change_analysis.checks.loader import load_from_source, validate_rows
        from change_analysis.db.mart import Mart

        check = load_from_source(spec["code"], source_ref=spec.get("source_ref", "workspace"),
                                 source_kind=spec.get("source_kind", "workspace"))
        mart = Mart(spec["run_id"])
        params = {**check.params, **(spec.get("params") or {})}
        rows = check.entrypoint(mart, params)
        known = _run_app_ids(spec["run_id"])
        rows = validate_rows(rows or [], spec["run_id"], known_app_ids=known)
        (out_dir / "rows.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
        result["rows"] = len(rows)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - always report
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-4000:]
    finally:
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
