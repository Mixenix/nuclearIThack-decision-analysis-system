"""A small, cheap aggregate used as the model's input packet.

Deliberately not the analyst summary: the model gets numbers that are already
deterministic, with no per-application payload beyond a bounded evidence sample.
"""

from __future__ import annotations

from typing import Any

from ..db.connection import query


def light_summary(run_id: str, revision: int) -> dict[str, Any]:
    row = query(
        """
        SELECT
          (SELECT count(*) FROM ca.app_segments WHERE run_id = %(run)s) AS pairs,
          (SELECT count(DISTINCT app_id) FROM ca.comparison_rows
            WHERE run_id = %(run)s AND diff_flag = 1) AS changed_apps,
          (SELECT count(*) FROM ca.app_segments
            WHERE run_id = %(run)s AND decision_changed = 1) AS decision_flips,
          (SELECT count(*) FROM ca.app_segments
            WHERE run_id = %(run)s AND decision_before = 'APPROVE') AS approve_before,
          (SELECT count(*) FROM ca.app_segments
            WHERE run_id = %(run)s AND decision_after = 'APPROVE') AS approve_after,
          (SELECT count(*) FROM ca.comparison_rows
            WHERE run_id = %(run)s AND change_type = 'ATTRIBUTE_NEW') AS attribute_new,
          (SELECT count(*) FROM ca.comparison_rows
            WHERE run_id = %(run)s AND change_type = 'ATTRIBUTE_LOST') AS attribute_lost,
          (SELECT count(*) FROM ca.comparison_rows
            WHERE run_id = %(run)s AND change_type = 'VALUE_CHANGED') AS value_changed
        """,
        {"run": run_id},
    )[0]
    pairs = int(row["pairs"] or 0)
    causes = query(
        "SELECT coalesce(nullif(cause_category, ''), 'UNKNOWN') AS cause, count(*) AS n"
        " FROM ca.app_segments WHERE run_id = %s GROUP BY 1 ORDER BY n DESC",
        (run_id,),
    )
    return {
        "pairs": pairs,
        "changed_apps": int(row["changed_apps"] or 0),
        "changed_share": round(int(row["changed_apps"] or 0) / pairs, 4) if pairs else None,
        "decision_flips": int(row["decision_flips"] or 0),
        "approval_rate_before": round(100.0 * int(row["approve_before"] or 0) / pairs, 2)
        if pairs else None,
        "approval_rate_after": round(100.0 * int(row["approve_after"] or 0) / pairs, 2)
        if pairs else None,
        "rows": {
            "VALUE_CHANGED": int(row["value_changed"] or 0),
            "ATTRIBUTE_NEW": int(row["attribute_new"] or 0),
            "ATTRIBUTE_LOST": int(row["attribute_lost"] or 0),
        },
        "cause_categories": {r["cause"]: int(r["n"]) for r in causes},
        "revision": revision,
    }
