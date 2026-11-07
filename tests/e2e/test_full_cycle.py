"""The whole cycle: dataset → run → report → pivot → drill-down → HTML.

This is the acceptance path of the task card, executed against the run the session
fixture produced with the real pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from change_analysis.contracts.types import FilterSpec
from change_analysis.db.connection import tx
from change_analysis.db.mart import Mart
from change_analysis.services import checks as checks_service
from change_analysis.services import report, runs
from change_analysis.services.errors import InvalidRequest

from qa_support import REFERENCE

pytestmark = pytest.mark.e2e


def read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def test_the_run_finished_and_its_numbers_match_the_reference(completed_run, owner):
    run = runs.get_run(owner, completed_run["run_id"])
    assert run["state"] == "SUCCEEDED"
    assert run["expected_pairs"] == REFERENCE["pairs"]
    # ``processed_pairs`` is a *stage* progress counter: the aggregate stage counts
    # comparison rows, so it is not the number of pairs (pages_run.py documents
    # this and never labels it as pairs).  The authoritative pair count is the
    # summary written at the end of the run.
    assert run["processed_pairs"] > 0
    assert isinstance(run["processed_pairs"], int)
    summary = run["summary"]
    assert summary["pairs"] == REFERENCE["pairs"]
    assert summary["rows"] == REFERENCE["rows"]
    assert summary["changed"] == REFERENCE["changed"]
    assert summary["decision_changed"] == REFERENCE["decision_changed"]
    assert run["error_message"] in (None, "")


def test_the_stage_log_names_the_code_that_actually_ran(completed_run, owner):
    stages = runs.run_stages(owner, completed_run["run_id"])
    by_stage = {stage["stage"]: stage for stage in stages}
    assert {"normalize", "compare", "aggregate"} <= set(by_stage)
    for stage in ("normalize", "compare", "aggregate"):
        row = by_stage[stage]
        assert row["state"] == "DONE", f"{stage} did not finish"
        assert row["procedure_kind"] in ("file", "workspace")
        assert row["procedure_ref"], f"{stage} does not point at a procedure"
        assert row["procedure_hash"], f"{stage} has no code hash"
        assert row["finished_at"] is not None and row["started_at"] is not None
        assert row["code_changed_since_run"] is False


def test_every_stage_reports_real_progress(completed_run, owner):
    """«прогресс виден, понятно, что происходит»: counters are filled, not zero."""
    stages = runs.run_stages(owner, completed_run["run_id"])
    done = [stage for stage in stages if stage["state"] == "DONE"]
    assert len(done) >= 3
    for stage in done:
        assert stage["progress_total"] > 0, f"{stage['stage']} reports no total"
        assert stage["progress_done"] > 0, f"{stage['stage']} reports no progress"
        assert stage["progress_done"] <= stage["progress_total"] * 1.0 or \
            stage["stage"] == "aggregate", (
                "the aggregate stage counts comparison rows, which exceeds the "
                "pair total on purpose (pages_run.py documents this)"
            )


def test_the_event_log_is_ordered_and_complete(completed_run, owner):
    events = runs.run_events(owner, completed_run["run_id"], limit=500)
    assert events
    ids = [event["id"] for event in events]
    assert ids == sorted(ids)
    messages = " ".join(event["message"] for event in events)
    for stage in ("normalize", "compare", "aggregate"):
        assert f"stage {stage} started" in messages
        assert f"stage {stage} finished" in messages
    assert "run finished" in messages


# ---------------------------------------------------------------------------
# the check report
# ---------------------------------------------------------------------------


def test_the_check_report_is_one_table_with_every_delivered_check(completed_run, owner):
    view = checks_service.check_report(owner, completed_run["run_id"])
    assert view["revision"] == 1
    assert len(view["rows"]) > 0
    assert {row["check_id"] for row in view["rows"]} == set(completed_run["check_ids"])
    assert view["counts"]["errors"] == 0
    assert view["scope"] == "Весь прогон; проверки не пересчитаны по фильтрам интерфейса"
    for row in view["rows"]:
        assert row["RESULT"] in ("PASS", "FAIL")
        assert row["CONFIDENCE"] in ("HIGH", "LOW")
        assert row["execution_state"] == "DONE"


def test_low_confidence_is_distinguishable_from_high_without_colour(synthetic_run, owner):
    """PLAN.md §21.3: the difference must not rest on colour alone.

    The delivered run has no LOW-FAIL row (every delivered check either passes or
    fires more than ``low_confidence_cnt`` times), so the rendering requirement is
    checked on a run that publishes one LOW-FAIL and one HIGH-FAIL row.
    """
    from qa_support import insert_check_result

    base = {"LABEL": "QA: подсветка уверенности", "SOURCE": "AFTER", "TOTAL": 100,
            "METRIC": 0.02}
    run_id = synthetic_run(
        name="qa-confidence",
        rows=[comparison_row("x", "app-1", "p.q", value_before="1", value_after="2",
                             change_type="VALUE_CHANGED")],
        segments=[segment_row("x", "app-1")],
    )
    insert_check_result(tx, run_id, 1, "qa_confidence", "QA: подсветка уверенности", rows=[
        dict(base, ARGUMENT="слабый сигнал", CNT=2, APP_IDS=["app-1"],
             RESULT="FAIL", CONFIDENCE="LOW"),
        dict(base, ARGUMENT="сильный сигнал", CNT=42, APP_IDS=["app-1"],
             RESULT="FAIL", CONFIDENCE="HIGH"),
        dict(base, ARGUMENT="норма", CNT=0, APP_IDS=[], METRIC=None,
             RESULT="PASS", CONFIDENCE="HIGH"),
    ])

    view = checks_service.check_report(owner, run_id)
    assert view["counts"] == {"rows": 3, "high_fail": 1, "low_fail": 1, "passed": 1,
                              "errors": 0}
    html = read_html(report.export_html(owner, run_id))
    assert html.count("FAIL · низкая уверенность") == 1
    # the class is checked on the table row, not on the stylesheet (which also
    # mentions the selector)
    assert html.count('<tr class="fail-low">') == 1
    assert html.count('<tr class="fail-high">') == 1
    assert "Срабатывания проверок (HIGH / LOW)" in html


# ---------------------------------------------------------------------------
# drill-down
# ---------------------------------------------------------------------------


def test_a_cell_number_equals_the_size_of_its_application_set(completed_run, owner):
    run_id = completed_run["run_id"]
    spec = FilterSpec(run_id=run_id)
    view = report.pivot(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec)
    for cell_key, cell in view["cells"].items():
        first = report.drill(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec, cell_key,
                             page=1, page_size=1000)
        assert first["total"] == cell["cnt"]
        collected = list(first["app_ids"])
        page = 1
        while len(collected) < first["total"]:
            page += 1
            more = report.drill(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec,
                                cell_key, page=page, page_size=1000)
            assert more["app_ids"], "paging stopped before the cell was exhausted"
            collected.extend(more["app_ids"])
        assert len(collected) == len(set(collected)) == cell["cnt"]
        assert set(collected) <= set(Mart(run_id).app_ids())


def test_the_application_card_opens_from_a_drilled_application(completed_run, owner):
    run_id = completed_run["run_id"]
    app_id = Mart(run_id).app_ids(diff_flag=1)[0]
    detail = report.app_detail(owner, run_id, app_id)
    segment = detail["segments"][0]
    assert segment["decision_changed"] in (0, 1)
    assert detail["products"], "the product layer must not be empty for a changed app"
    attributes = report.attribute_rows(owner, run_id, app_id)
    assert attributes
    assert all(row["diff_flag"] == 1 for row in attributes)
    assert any(row["change_type"] in ("VALUE_CHANGED", "ATTRIBUTE_NEW", "ATTRIBUTE_LOST")
               for row in attributes)


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------


def test_export_writes_a_file_under_the_artifact_root(completed_run, owner, settings):
    path = report.export_html(owner, completed_run["run_id"])
    assert Path(path).is_file()
    assert Path(path).resolve().is_relative_to(settings.artifact_root.resolve())
    assert Path(path).stat().st_size > 2000


def test_export_is_standalone_and_links_back_to_the_application(completed_run, owner,
                                                               settings):
    run_id = completed_run["run_id"]
    html = read_html(report.export_html(owner, run_id))
    base = settings.public_base_url

    assert "<style>" in html, "the styling must be inlined"
    assert "rel=\"stylesheet\"" not in html
    assert not re.search(r"<link\b", html, re.I)
    assert not re.search(r"<script\b", html, re.I)

    urls = set(re.findall(r"https?://[^\s\"'<>]+", html))
    assert urls, "the document must link back to the application"
    assert all(url.startswith(base) for url in urls), sorted(urls)[:5]
    for route in ("", "/report", "/explore"):
        assert f"href=\"{base}/runs/{run_id}{route}\"" in html


def test_export_contains_the_real_numbers(completed_run, owner):
    """Every number in the document must be the number the application computed."""
    run_id = completed_run["run_id"]
    html = read_html(report.export_html(owner, run_id))
    view = checks_service.check_report(owner, run_id)

    for value in (str(REFERENCE["pairs"]), str(REFERENCE["changed"]),
                  str(REFERENCE["decision_changed"])):
        assert value in html, f"{value} is missing from the exported report"

    # the check table is rendered row by row, with the same labels the service has
    for row in view["rows"]:
        assert row["LABEL"] in html
        assert f"{row['CNT']} / {row['TOTAL']}" in html
    assert html.count("Проверки вычислены по всему прогону") == 1
    assert "Сводка" in html and "Результаты автоматических проверок" in html
    assert "Ограничения" in html


def test_export_shows_user_text_as_text(completed_run, owner):
    """A run name containing markup must be escaped, not executed (PLAN.md §21.3)."""
    from conftest import INJECTION

    html = read_html(report.export_html(owner, completed_run["run_id"]))
    assert INJECTION not in html, "raw user markup reached the document"
    escaped = INJECTION.replace("<", "&lt;").replace(">", "&gt;")
    assert escaped in html


def test_export_refuses_an_unknown_revision(completed_run, owner):
    with pytest.raises(InvalidRequest):
        report.export_html(owner, completed_run["run_id"], revision=99)
