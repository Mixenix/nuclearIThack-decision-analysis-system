"""The merged check report.

Business rows and technical execution state stay in separate structures: a check
that crashed is an execution ``ERROR``, never a business ``FAIL`` (PLAN.md §17.1).
"""

from __future__ import annotations

from typing import Any

from ..db.connection import query
from .errors import InvalidRequest
from .report import _owned

_ORDER = (
    "CASE WHEN e.state <> 'DONE' THEN 3"
    " WHEN r.result = 'FAIL' AND r.confidence = 'HIGH' THEN 0"
    " WHEN r.result = 'FAIL' THEN 1 ELSE 2 END, r.id"
)


def check_report(user_id: str, run_id: str, revision: int | None = None) -> dict[str, Any]:
    """Owner-checked entry point used by the HTTP layer."""
    run = _owned(user_id, run_id)
    return build_check_report(run_id, revision, max_revision=int(run["revision"]))


def build_check_report(run_id: str, revision: int | None = None,
                       max_revision: int | None = None) -> dict[str, Any]:
    """The report itself, without an ownership check.

    Used by the summary, which has already checked ownership of the same run —
    this avoids either duplicating the queries or monkeypatching the guard.
    """
    if max_revision is None:
        rows = query("SELECT revision FROM ca.runs WHERE id = %s", (run_id,))
        if not rows:
            raise InvalidRequest("прогон не найден")
        max_revision = int(rows[0]["revision"])
    if revision is None:
        # Show the newest *published* revision: a report revision that still has a
        # running check is not ready, and the previous one must stay readable.
        published = query(
            "SELECT max(revision) AS rev FROM ca.check_executions"
            " WHERE run_id = %s AND state <> 'RUNNING'", (run_id,),
        )
        rev = int(published[0]["rev"]) if published and published[0]["rev"] else max_revision
    else:
        rev = revision
    if not isinstance(rev, int) or not 1 <= rev <= max_revision:
        raise InvalidRequest("неизвестная ревизия")

    executions = query(
        "SELECT id, check_id, label, source_kind, source_ref, state, duration_ms,"
        " row_count, error, params FROM ca.check_executions"
        " WHERE run_id = %s AND revision = %s ORDER BY id", (run_id, rev),
    )
    raw = query(
        "SELECT r.label, r.source, r.argument, r.cnt, r.total, r.metric, r.app_ids,"
        " r.result, r.confidence, r.id, r.execution_id, e.check_id,"
        " e.state AS execution_state, e.error AS execution_error, e.duration_ms"
        " FROM ca.check_results r JOIN ca.check_executions e ON e.id = r.execution_id"
        " WHERE r.run_id = %s AND r.revision = %s ORDER BY " + _ORDER,
        (run_id, rev),
    )

    rows: list[dict[str, Any]] = []
    for row in raw:
        app_ids = [str(a) for a in (row["app_ids"] or [])]
        rows.append({
            "LABEL": row["label"],
            "SOURCE": row["source"],
            "ARGUMENT": row["argument"],
            "CNT": int(row["cnt"]),
            "TOTAL": int(row["total"]),
            "METRIC": row["metric"],
            "APP_IDS": app_ids,
            "RESULT": row["result"],
            "CONFIDENCE": row["confidence"],
            "id": row["id"],
            "execution_id": row["execution_id"],
            "check_id": row["check_id"],
            "execution_state": row["execution_state"],
            "execution_error": row["execution_error"],
            "duration_ms": row["duration_ms"],
            # APP_IDS is capped by the check's own app_ids_limit, so CNT can be
            # larger. Never call the list complete (task package §"APP_IDS").
            "app_ids_are_sample": len(app_ids) < int(row["cnt"]),
            "scope": "Весь прогон; проверки не пересчитаны по фильтрам интерфейса",
        })

    return {
        "run_id": str(run_id),
        "revision": rev,
        "rows": rows,
        "executions": executions,
        "errors": [e for e in executions if e["state"] == "ERROR"],
        "scope": "Весь прогон; проверки не пересчитаны по фильтрам интерфейса",
        "counts": {
            "rows": len(rows),
            "high_fail": sum(1 for r in rows if r["RESULT"] == "FAIL"
                             and r["CONFIDENCE"] == "HIGH"
                             and r["execution_state"] == "DONE"),
            "low_fail": sum(1 for r in rows if r["RESULT"] == "FAIL"
                            and r["CONFIDENCE"] == "LOW"
                            and r["execution_state"] == "DONE"),
            "passed": sum(1 for r in rows if r["RESULT"] == "PASS"
                          and r["execution_state"] == "DONE"),
            "errors": len([e for e in executions if e["state"] == "ERROR"]),
        },
    }
