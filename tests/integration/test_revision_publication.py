"""Publication of a new check revision keeps the previous one readable (real SQL).

Moved out of ``tests/unit`` because it needs PostgreSQL: the unit suite must run
with no database at all, which is what the README promises.
"""

from __future__ import annotations

import pytest

from change_analysis.contracts.types import FilterSpec
from change_analysis.services import report
from change_analysis.services.errors import InvalidRequest

@pytest.mark.integration
def test_revision_three_export_and_pending_four_on_test_database(synthetic_run, owner):
    """Real SQL proof; conftest forces change_analysis_test and removes this run."""
    from pathlib import Path

    from change_analysis.db.connection import execute, tx
    from qa_support import comparison_row, insert_check_result, segment_row

    run_id = synthetic_run(
        name="qa-demo-consistency", rows=[comparison_row(
            "", "app", "decisionResult.model", change_type="VALUE_CHANGED")],
        segments=[segment_row("", "app")],
        facts=[dict(path="decisionResult.model", diff_flag=1, change_type="VALUE_CHANGED",
                    source="AFTER", cnt=1, app_ids=["app"])],
    )
    row = dict(LABEL="check", SOURCE="AFTER", CNT=1, TOTAL=1,
               RESULT="PASS", CONFIDENCE="HIGH", APP_IDS=["app"])
    insert_check_result(tx, run_id, 1, "old", "old", rows=[row])
    insert_check_result(tx, run_id, 3, "new", "new", rows=[
        dict(row, LABEL="new high", RESULT="FAIL"),
        dict(row, LABEL="new low", RESULT="FAIL", CONFIDENCE="LOW"),
    ])
    execute("UPDATE ca.runs SET revision=3 WHERE id=%s", (run_id,))
    path = report.export_html(owner, run_id)
    html = Path(path).read_text()
    assert path.endswith("report-rev3.html")
    assert "ревизия 3" in html and "витрина 1" in html
    assert html.count('<td class="verdict ') == 2
    assert '<div class="value">1 / 1</div>' in html
    assert "во всём прогоне: 1." in html
    assert report.pivot(owner, run_id, [], [], FilterSpec(
        run_id=run_id, decision_changed=False, diff_flag=1))["grand_total"] == 1
    # Mixed DONE/RUNNING executions must not expose a partially published report.
    execute("UPDATE ca.runs SET revision=4 WHERE id=%s", (run_id,))
    insert_check_result(tx, run_id, 4, "first", "first", rows=[row])
    insert_check_result(tx, run_id, 4, "second", "second", state="RUNNING")
    assert report.published_check_report(owner, run_id)["revision"] == 3
    assert report.export_html(owner, run_id).endswith("report-rev3.html")
    with pytest.raises(InvalidRequest, match="не опубликована"):
        report.export_html(owner, run_id, 4)
    print("TEST DB EXPORT: revision=3, data_revision=1, rows=2, HIGH/LOW=1/1; pending 4 rejected")
