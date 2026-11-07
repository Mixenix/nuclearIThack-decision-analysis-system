"""The five delivered checks, executed unmodified against a real run.

The acceptance requirement is literal: «выданные проверки подключаются и
исполняются» and «пять исходных файлов исполняются без изменения».  The hashes
below are taken before and after execution, so a rewritten copy anywhere in the
path would fail the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from change_analysis.checks.loader import CheckError, execute, list_builtin_checks
from change_analysis.db.mart import Mart

from qa_support import sha256_file

pytestmark = pytest.mark.integration

NEW_CHECK_SOURCE = '''
"""A check nobody delivered: same contract, written here to prove it is enough."""

DEFAULTS = {"app_ids_limit": 5}


def run(mart, params=None):
    p = {**DEFAULTS, **(params or {})}
    total = mart.total_apps()
    paths = {}
    for row in mart.rows(diff_flag=1):
        paths.setdefault(row["PATH"], set()).add(row["APP_ID"])
    worst = sorted(paths.items(), key=lambda item: (-len(item[1]), item[0]))[:3]
    rows = []
    for path, apps in worst:
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
    return rows


CHECK = {"id": "qa_top_changed_paths",
         "label": "QA: самые массовые изменённые атрибуты",
         "params": DEFAULTS,
         "entrypoint": run}
'''


def check_files() -> list[Path]:
    return [Path(spec.source_ref) for spec in list_builtin_checks()]


def test_the_five_files_are_byte_identical_before_and_after_execution(completed_run):
    specs = list_builtin_checks()
    before = {spec.id: sha256_file(spec.source_ref) for spec in specs}

    for spec in specs:
        result = execute(spec, completed_run["run_id"])
        assert result["state"] == "DONE", f"{spec.id}: {result['error']}"

    after = {spec.id: sha256_file(spec.source_ref) for spec in specs}
    assert after == before, "a delivered check file was modified by executing it"


def test_checked_code_equals_the_file_on_disk(completed_run):
    for spec in list_builtin_checks():
        on_disk = Path(spec.source_ref).read_text(encoding="utf-8")
        assert spec.code == on_disk, f"{spec.id} executes text that is not the file"
        assert Path(spec.source_ref).name == f"check_{spec.id}.py"


def test_every_delivered_check_executes_and_returns_legal_rows(completed_run):
    run_id = completed_run["run_id"]
    mart = Mart(run_id)
    known = {s["APP_ID"] for s in mart.segments()}
    per_check: dict[str, int] = {}

    for spec in list_builtin_checks():
        result = execute(spec, run_id)
        assert result["state"] == "DONE", f"{spec.id} failed: {result['error']}"
        assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
        per_check[spec.id] = len(result["rows"])
        for row in result["rows"]:
            assert row["LABEL"] == spec.label
            assert row["SOURCE"] in ("BEFORE", "AFTER")
            assert row["RESULT"] in ("PASS", "FAIL")
            assert row["CONFIDENCE"] in ("HIGH", "LOW")
            assert row["CNT"] >= 0 and row["TOTAL"] >= 0
            assert set(row["APP_IDS"]) <= known, "APP_IDS outside this run"
            assert len(row["APP_IDS"]) <= int(spec.params["app_ids_limit"])
            assert len(row["APP_IDS"]) <= row["CNT"] or row["CNT"] == 0
            if row["CONFIDENCE"] == "LOW":
                assert row["CNT"] < int(spec.params["low_confidence_cnt"])

    assert set(per_check) == {spec.id for spec in list_builtin_checks()}
    assert sum(per_check.values()) > 0


def test_merged_report_contains_every_check(completed_run):
    from change_analysis.services.checks import build_check_report

    report = build_check_report(completed_run["run_id"], 1, max_revision=1)
    labels_by_check = {spec.id: spec.label for spec in list_builtin_checks()}
    seen = {row["check_id"] for row in report["rows"]}
    assert seen == set(labels_by_check)
    assert report["counts"]["rows"] == len(report["rows"])
    assert report["counts"]["errors"] == 0
    assert report["counts"]["high_fail"] + report["counts"]["low_fail"] \
        + report["counts"]["passed"] == report["counts"]["rows"]
    # the report is scoped to the whole run and says so
    assert "весь прогон" in report["scope"].lower()
    for row in report["rows"]:
        assert row["scope"] == report["scope"]
        assert row["app_ids_are_sample"] == (len(row["APP_IDS"]) < row["CNT"])


def test_approval_rate_numbers_reproduce_from_the_mart(completed_run):
    """Independent recomputation from the MartAPI, not a copy of the check body."""
    mart = Mart(completed_run["run_id"])
    total = mart.total_apps()
    stats: dict[str, dict[str, int]] = {}
    for segment in mart.segments():
        for product in (segment["PRODUCTS"] or "").split(";"):
            if not product:
                continue
            bucket = stats.setdefault(product, {"total": 0, "lost": 0})
            bucket["total"] += 1
            if segment["DECISION_BEFORE"] == "APPROVE" and segment["DECISION_AFTER"] != "APPROVE":
                bucket["lost"] += 1

    spec = {s.id: s for s in list_builtin_checks()}["ar_drop_by_product"]
    result = execute(spec, completed_run["run_id"])
    assert result["state"] == "DONE"
    assert result["rows"], "the delivered dataset must produce product rows"
    for row in result["rows"]:
        expected = stats[row["ARGUMENT"]]
        # TOTAL is the number of applications that contain this product, not the
        # number of applications in the run (the delivered check counts per product)
        assert row["TOTAL"] == expected["total"]
        assert row["CNT"] == expected["lost"]
        assert 0 < row["TOTAL"] <= total


def test_attribute_new_loss_numbers_reproduce_from_the_mart(completed_run):
    mart = Mart(completed_run["run_id"])
    total = mart.total_apps()
    spec = {s.id: s for s in list_builtin_checks()}["attribute_new_loss"]
    result = execute(spec, completed_run["run_id"])
    assert result["state"] == "DONE"
    for row in result["rows"]:
        path = row["ARGUMENT"]
        if not path:
            assert row["RESULT"] == "PASS" and row["CNT"] == 0
            continue
        change = "ATTRIBUTE_NEW" if row["SOURCE"] == "AFTER" else "ATTRIBUTE_LOST"
        apps = mart.app_ids(path=path, change_type=change)
        assert row["CNT"] == len(apps)
        assert row["TOTAL"] == total


def test_execution_is_deterministic(completed_run):
    spec = {s.id: s for s in list_builtin_checks()}["stop_factor_disappeared"]
    first = execute(spec, completed_run["run_id"])
    second = execute(spec, completed_run["run_id"])
    assert first["state"] == second["state"] == "DONE"
    assert first["rows"] == second["rows"]


# ---------------------------------------------------------------------------
# an unknown check, written by "the user", works by the same contract
# ---------------------------------------------------------------------------


def test_a_new_check_written_by_a_user_executes_through_the_same_path(completed_run):
    from change_analysis.checks.loader import load_from_source

    run_id = completed_run["run_id"]
    spec = load_from_source(NEW_CHECK_SOURCE, "qa_top_changed_paths.py")
    assert spec.id == "qa_top_changed_paths"

    result = execute(spec, run_id)
    assert result["state"] == "DONE", result["error"]
    rows = result["rows"]
    assert len(rows) == 3
    assert [r["CNT"] for r in rows] == sorted((r["CNT"] for r in rows), reverse=True)

    mart = Mart(run_id)
    for row in rows:
        assert row["CNT"] == len(mart.app_ids(path=row["ARGUMENT"], diff_flag=1))
        assert row["TOTAL"] == mart.total_apps()
        assert len(row["APP_IDS"]) <= 5


def test_a_crashing_check_is_a_technical_error_not_a_business_fail(completed_run):
    from change_analysis.checks.loader import load_from_source

    source = (
        "def run(mart, params=None):\n"
        "    return 1 / 0\n"
        "CHECK = {'id': 'qa_crash', 'label': 'QA crash', 'params': {},"
        " 'entrypoint': run}\n"
    )
    spec = load_from_source(source, "qa_crash.py")
    result = execute(spec, completed_run["run_id"])
    assert result["state"] == "ERROR"
    assert result["rows"] == []
    assert "ZeroDivisionError" in result["error"]


def test_an_illegal_row_turns_the_check_into_a_technical_error(completed_run):
    from change_analysis.checks.loader import load_from_source

    source = (
        "def run(mart, params=None):\n"
        "    return [{'LABEL': 'x', 'SOURCE': 'SIDEWAYS', 'RESULT': 'FAIL',\n"
        "             'CONFIDENCE': 'HIGH', 'CNT': 1, 'TOTAL': 1, 'APP_IDS': []}]\n"
        "CHECK = {'id': 'qa_illegal', 'label': 'QA illegal', 'params': {},"
        " 'entrypoint': run}\n"
    )
    spec = load_from_source(source, "qa_illegal.py")
    result = execute(spec, completed_run["run_id"])
    assert result["state"] == "ERROR"
    assert "SOURCE" in result["error"]


def test_check_files_are_not_written_to_by_the_suite():
    """No test in this suite is allowed to save a check next to the delivered ones."""
    checks_dir = Path(check_files()[0]).parent
    names = sorted(path.name for path in checks_dir.glob("*.py"))
    assert names == sorted(f"check_{spec.id}.py" for spec in list_builtin_checks())
