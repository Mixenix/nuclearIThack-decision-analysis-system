"""«Прогнать проверку на готовом прогоне» — the CHECKS_ONLY path.

The demo scenario of the task card is: open a finished run, write a new check in
the workspace, apply it, and see it in *the same* report.  That means the
checks-only job must (1) not recompute the mart, (2) not duplicate rows, and
(3) keep everything the run already published — the earlier check results, the
pivot facts and the summary.

The run used here is synthetic on purpose (a handful of rows): a second full
2002-application run would cost two minutes and prove nothing extra about this
code path.  The expensive full run is exercised by ``test_full_cycle.py``.
"""

from __future__ import annotations

import json

import pytest

from change_analysis.checks.loader import list_builtin_checks
from change_analysis.contracts.types import FilterSpec
from change_analysis.db.connection import execute, query
from change_analysis.services import checks as checks_service
from change_analysis.services import procedures, report, runs
from change_analysis.services.errors import InvalidRequest
from change_analysis.worker.runner import PipelineJob, run_checks_only

from qa_support import comparison_row, segment_row

pytestmark = pytest.mark.e2e

NEW_CHECK_CODE = '''
"""A check the analyst writes during the demo: biggest changed attribute."""


def run(mart, params=None):
    p = {"app_ids_limit": 20, **(params or {})}
    total = mart.total_apps()
    counts = {}
    for row in mart.rows(diff_flag=1):
        counts.setdefault(row["PATH"], set()).add(row["APP_ID"])
    rows = []
    for path, apps in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rows.append({
            "LABEL": CHECK["label"],
            "SOURCE": "AFTER",
            "ARGUMENT": path,
            "CNT": len(apps),
            "TOTAL": total,
            "METRIC": round(len(apps) / total, 4) if total else 0.0,
            "APP_IDS": sorted(apps)[: p["app_ids_limit"]],
            "RESULT": "FAIL",
            "CONFIDENCE": "HIGH" if len(apps) >= 5 else "LOW",
        })
    if not rows:
        rows.append({"LABEL": CHECK["label"], "SOURCE": "AFTER", "ARGUMENT": "",
                     "CNT": 0, "TOTAL": total, "METRIC": 0.0, "APP_IDS": [],
                     "RESULT": "PASS", "CONFIDENCE": "HIGH"})
    return rows


CHECK = {"id": "qa_demo_check",
         "label": "QA: самый массовый изменённый атрибут",
         "params": {"app_ids_limit": 20},
         "entrypoint": run}
'''

APPS = ["app-1", "app-2", "app-3"]


def seed_rows(run_id):
    rows = []
    for index, app in enumerate(APPS, start=1):
        rows.append(comparison_row(run_id, app, "characteristics.score_okb",
                                   value_before=str(700 + index), value_after=str(680 + index),
                                   change_type="VALUE_CHANGED"))
        rows.append(comparison_row(run_id, app, "decisionResult.product_trials.rules.hit",
                                   array_key=f"15210|RULE_{index}",
                                   value_before="true", value_after="false",
                                   change_type="VALUE_CHANGED"))
    return rows


@pytest.fixture
def finished_run_with_checks(synthetic_run, settings, owner):
    """A SUCCEEDED run whose five delivered checks have already been published."""
    run_id = synthetic_run(
        name="qa-checks-only",
        rows=seed_rows("x"),
        segments=[segment_row("x", app) for app in APPS],
        facts=[
            {"cls": "characteristics", "path": "characteristics.score_okb",
             "key": "score_okb", "change_type": "VALUE_CHANGED", "diff_flag": 1,
             "source": "AFTER", "cnt": 3, "app_ids": APPS},
            {"cls": "decisionResult", "path": "decisionResult.product_trials.rules.hit",
             "key": "hit", "change_type": "VALUE_CHANGED", "diff_flag": 1,
             "source": "AFTER", "cnt": 3, "app_ids": APPS},
        ],
    )
    checks = [{"id": spec.id, "label": spec.label, "code": spec.code,
               "params": dict(spec.params), "source_kind": "file",
               "source_ref": spec.source_ref} for spec in list_builtin_checks()]
    # a real run freezes its configured checks in the snapshot at creation time;
    # CHECKS_ONLY merges into that list and republishes it, so the fixture must
    # declare the same set, otherwise revision 2 would legitimately contain only
    # the newly added check
    execute("UPDATE ca.runs SET snapshot = %s::jsonb WHERE id = %s",
            (json.dumps({"checks": checks}), run_id))
    PipelineJob({"id": 0, "run_id": run_id}, settings).run_checks(checks, revision=1)
    published = query(
        "SELECT count(*) AS n FROM ca.check_results WHERE run_id = %s AND revision = 1",
        (run_id,),
    )[0]["n"]
    assert published > 0
    return {"run_id": run_id, "published_rows": int(published)}


def report_signature(run_id, owner):
    view = checks_service.check_report(owner, run_id, revision=1)
    return sorted((row["check_id"], row["LABEL"], row["ARGUMENT"], row["CNT"],
                   row["RESULT"]) for row in view["rows"])


def apply_new_check(owner, run_id, settings):
    """The whole user path: save a check in the workspace, then run it on the run."""
    procedure_id = procedures.save_procedure(
        owner, "check", "QA: самый массовый изменённый атрибут", "workspace",
        code=NEW_CHECK_CODE,
    )
    try:
        job_id = runs.request_checks_only(
            owner, run_id,
            [{"id": f"proc:{procedure_id}", "source": "workspace",
              "procedure_id": procedure_id}],
        )
        job = query("SELECT id, run_id, kind, state FROM ca.jobs WHERE id = %s",
                    (int(job_id),))[0]
        assert job["kind"] == "CHECKS_ONLY"
        assert job["state"] == "QUEUED"
        run_checks_only(dict(job), settings)
        return procedure_id, job
    finally:
        procedures.delete_procedure(owner, procedure_id)


def test_checks_only_does_not_recompute_the_mart(finished_run_with_checks, owner, settings):
    run_id = finished_run_with_checks["run_id"]
    rows_before = query("SELECT count(*) AS n FROM ca.comparison_rows WHERE run_id = %s",
                        (run_id,))[0]["n"]
    segments_before = query("SELECT count(*) AS n FROM ca.app_segments WHERE run_id = %s",
                            (run_id,))[0]["n"]
    facts_before = query("SELECT count(*) AS n FROM ca.pivot_facts WHERE run_id = %s",
                         (run_id,))[0]["n"]

    _, job = apply_new_check(owner, run_id, settings)

    assert query("SELECT count(*) AS n FROM ca.comparison_rows WHERE run_id = %s",
                 (run_id,))[0]["n"] == rows_before
    assert query("SELECT count(*) AS n FROM ca.app_segments WHERE run_id = %s",
                 (run_id,))[0]["n"] == segments_before
    assert query("SELECT count(*) AS n FROM ca.pivot_facts WHERE run_id = %s",
                 (run_id,))[0]["n"] == facts_before
    finished = query("SELECT state FROM ca.jobs WHERE id = %s", (int(job["id"]),))[0]
    assert finished["state"] == "SUCCEEDED"


def test_checks_only_adds_the_new_check_to_the_same_report(finished_run_with_checks,
                                                           owner, settings):
    run_id = finished_run_with_checks["run_id"]
    before = {row["check_id"]
              for row in checks_service.check_report(owner, run_id, revision=1)["rows"]}

    procedure_id, _ = apply_new_check(owner, run_id, settings)

    revision = query("SELECT revision FROM ca.runs WHERE id = %s", (run_id,))[0]["revision"]
    assert revision == 2, "publishing a new report must bump the revision"
    after = checks_service.check_report(owner, run_id, revision=2)
    check_ids = {row["check_id"] for row in after["rows"]}
    assert f"proc:{procedure_id}" in check_ids
    assert before, "the run must have published the delivered checks first"
    new_rows = [row for row in after["rows"] if row["check_id"] == f"proc:{procedure_id}"]
    assert new_rows
    assert all(row["RESULT"] == "FAIL" for row in new_rows)
    assert sum(1 for _ in after["rows"]) == len(set(
        (row["check_id"], row["ARGUMENT"]) for row in after["rows"]
    )), "the report must not contain duplicated rows"


def test_checks_only_keeps_the_results_of_the_checks_run_before(finished_run_with_checks,
                                                                owner, settings):
    """The demo promise: «покажите, что новая проверка попала в тот же отчёт»."""
    run_id = finished_run_with_checks["run_id"]
    before = report_signature(run_id, owner)

    apply_new_check(owner, run_id, settings)

    after = report_signature(run_id, owner)
    assert before, "the run must have published results before the new check"
    missing = [row for row in before if row not in after]
    assert not missing, (
        f"{len(missing)} previously published check rows disappeared from revision 1: "
        f"{missing[:3]}"
    )
    # and the new revision repeats the whole configured set, not only the new check
    revision = query("SELECT revision FROM ca.runs WHERE id = %s", (run_id,))[0]["revision"]
    assert revision == 2
    republished = checks_service.check_report(owner, run_id, revision=2)
    assert {row["check_id"] for row in republished["rows"]} >= {row[0] for row in before}


def test_checks_only_keeps_the_pivot_and_the_summary(finished_run_with_checks, owner,
                                                     settings):
    run_id = finished_run_with_checks["run_id"]
    spec = FilterSpec(run_id=run_id)
    pivot_before = report.pivot(owner, run_id, ["CLASS"], [], spec)
    summary_before = report.summary(owner, run_id, spec)
    assert pivot_before["grand_total"] == len(APPS)
    assert summary_before["total_pairs"] == len(APPS)

    apply_new_check(owner, run_id, settings)

    pivot_after = report.pivot(owner, run_id, ["CLASS"], [], spec)
    summary_after = report.summary(owner, run_id, spec)
    assert pivot_after["grand_total"] == pivot_before["grand_total"]
    assert summary_after["total_pairs"] == summary_before["total_pairs"]
    assert summary_after["changed_apps"] == summary_before["changed_apps"]


def test_checks_only_is_refused_on_a_run_that_has_not_finished(synthetic_run, owner):
    run_id = synthetic_run(name="qa-queued-checks", state="QUEUED")
    with pytest.raises(InvalidRequest):
        runs.request_checks_only(owner, run_id, [])


def test_a_second_checks_only_does_not_drift(finished_run_with_checks, owner, settings):
    """Two CHECKS_ONLY jobs in a row: ``revision`` moves, ``data_revision`` must not."""
    run_id = finished_run_with_checks["run_id"]
    spec = FilterSpec(run_id=run_id)
    pivot_before = report.pivot(owner, run_id, ["CLASS"], [], spec)["grand_total"]
    summary_before = report.summary(owner, run_id, spec)["total_pairs"]
    data_revision_before = query("SELECT data_revision FROM ca.runs WHERE id = %s",
                                 (run_id,))[0]["data_revision"]

    apply_new_check(owner, run_id, settings)
    apply_new_check(owner, run_id, settings)

    row = query("SELECT revision, data_revision FROM ca.runs WHERE id = %s", (run_id,))[0]
    assert row["revision"] == 3, "each published report is a new revision"
    assert row["data_revision"] == data_revision_before, (
        "the datamart revision must not move when only checks are republished"
    )
    assert report.pivot(owner, run_id, ["CLASS"], [], spec)["grand_total"] == pivot_before
    assert report.summary(owner, run_id, spec)["total_pairs"] == summary_before

    # both earlier revisions stay readable
    for revision in (1, 2):
        view = checks_service.check_report(owner, run_id, revision=revision)
        assert view["rows"], f"revision {revision} lost its rows"
        ids = [(r["check_id"], r["ARGUMENT"]) for r in view["rows"]]
        assert len(ids) == len(set(ids))
    # the latest revision repeats the whole configured set
    latest = checks_service.check_report(owner, run_id, revision=3)
    assert {r["check_id"] for r in latest["rows"]} >= {
        r["check_id"] for r in checks_service.check_report(owner, run_id, revision=1)["rows"]
    }
    assert len(latest["rows"]) == len({(r["check_id"], r["ARGUMENT"]) for r in latest["rows"]})
