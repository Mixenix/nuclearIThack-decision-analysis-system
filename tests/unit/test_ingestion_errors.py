"""Broken input must fail loudly and name the culprit (PLAN.md §7, §21.1).

«Повреждённый JSON / неполная пара → FAILED, с указанием имени файла / APP_ID».
A batch run that silently drops applications is worse than a crash, so the
worker refuses to start when pairs are incomplete.

Diagnostics produced while streaming rows must reach ``result.json`` and
errors must fail the normalize stage.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from change_analysis.config import Settings
from change_analysis.procedures import spr_normalize
from change_analysis.worker import stage_runner
from change_analysis.worker.runner import PipelineJob, StageFailed

from qa_support import FakeContext

pytestmark = pytest.mark.unit


GOOD_DOC = {
    "appData": {"application_id": "app-1", "request_id": "rid-1"},
    "characteristics": {"score_okb": 700},
    "decisionResult": {"decision": "APPROVE"},
}

def write_dataset(root: Path, app_id: str, before: str, after: str) -> dict[str, str]:
    before_dir = root / "RESULTS" / "BEFORE_MASTER"
    after_dir = root / "RESULTS" / "AFTER_BETA"
    before_dir.mkdir(parents=True, exist_ok=True)
    after_dir.mkdir(parents=True, exist_ok=True)
    (before_dir / f"{app_id}.json").write_text(before, encoding="utf-8")
    (after_dir / f"{app_id}.json").write_text(after, encoding="utf-8")
    return {"app_id": app_id,
            "before": str(before_dir / f"{app_id}.json"),
            "after": str(after_dir / f"{app_id}.json")}


def drain(result) -> list[dict]:
    return list(result.rows)


# ---------------------------------------------------------------------------
# the normalize procedure: what happens to a broken pair
# ---------------------------------------------------------------------------


def test_broken_json_pair_is_skipped_and_the_run_continues(tmp_path):
    good = write_dataset(tmp_path, "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    broken = write_dataset(tmp_path, "app-2", "{not json", json.dumps(GOOD_DOC))
    context = FakeContext(tmp_path / "art")

    result = spr_normalize.run(context, {"apps": [good, broken]}, {})
    rows = drain(result)

    assert {r["app_id"] for r in rows} == {"app-1"}
    assert [s["APP_ID"] for s in result.segments] == ["app-1"]


def test_app_id_mismatch_keeps_the_file_name_as_identity(tmp_path):
    doc = json.loads(json.dumps(GOOD_DOC))
    doc["appData"]["application_id"] = "somebody-else"
    pair = write_dataset(tmp_path, "app-1", json.dumps(doc), json.dumps(doc))
    context = FakeContext(tmp_path / "art")

    result = spr_normalize.run(context, {"apps": [pair]}, {})
    drain(result)

    assert [s["APP_ID"] for s in result.segments] == ["app-1"]


def test_undeclared_array_costs_only_that_slice(tmp_path):
    doc = json.loads(json.dumps(GOOD_DOC))
    doc["brand_new_array"] = [1, 2, 3]
    pair = write_dataset(tmp_path, "app-1", json.dumps(doc), json.dumps(GOOD_DOC))
    context = FakeContext(tmp_path / "art")

    result = spr_normalize.run(context, {"apps": [pair]}, {})
    rows = drain(result)

    assert {r["source"] for r in rows} == {"AFTER"}, (
        "the unreadable BEFORE slice must not be replaced by the AFTER one"
    )
    assert len(result.segments) == 1


# ---------------------------------------------------------------------------
# ... and the diagnostic that must accompany it
# ---------------------------------------------------------------------------


def run_stage_runner(tmp_path: Path, apps: list[dict], params: dict | None = None) -> dict:
    """Run the real computing subprocess entry point in-process."""
    from change_analysis.config import APP_ROOT

    out_dir = tmp_path / "out"
    spec = {
        "stage": "normalize",
        "run_id": str(uuid.uuid4()),
        "backend": "cpu",
        "out_dir": str(out_dir),
        "code_path": str(APP_ROOT / "src" / "change_analysis" / "procedures"
                         / "spr_normalize.py"),
        "params": params or {},
        "inputs": {"apps": apps, "dataset_root": str(tmp_path)},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    # argv[0] is the module path, as in "python -m change_analysis.worker.stage_runner"
    exit_code = stage_runner.main(["stage_runner", str(spec_path)])
    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert exit_code == (0 if result["ok"] else 1)
    return result


def test_stage_runner_reports_failure_and_diagnostic_for_a_broken_pair(tmp_path):
    """A partial rows artifact must never turn a broken batch into success."""
    good = write_dataset(tmp_path, "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    broken = write_dataset(tmp_path, "app-2", "{not json", json.dumps(GOOD_DOC))

    result = run_stage_runner(tmp_path, [good, broken])

    assert result["ok"] is False
    assert result["counts"]["rows"] > 0
    assert result["diagnostics"][0]["code"] == "BAD_JSON"
    assert "app-2" in result["error"]
    assert broken["before"] in result["error"]
    rows = [json.loads(line) for line in
            (tmp_path / "out" / "rows.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert {r["app_id"] for r in rows} == {"app-1"}


def test_stage_runner_must_report_the_broken_pair_with_its_app_id(tmp_path):
    good = write_dataset(tmp_path, "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    broken = write_dataset(tmp_path, "app-2", "{not json", json.dumps(GOOD_DOC))

    result = run_stage_runner(tmp_path, [good, broken])

    diagnostics = result["diagnostics"]
    assert diagnostics, "a broken pair must produce a diagnostic"
    assert diagnostics[0]["code"] == "BAD_JSON"
    assert diagnostics[0]["where"] == "app-2"


def test_procedure_must_report_the_undeclared_array_with_path_and_slice(tmp_path):
    doc = json.loads(json.dumps(GOOD_DOC))
    doc["brand_new_array"] = [1, 2, 3]
    pair = write_dataset(tmp_path, "app-1", json.dumps(doc), json.dumps(GOOD_DOC))
    context = FakeContext(tmp_path / "art")

    result = spr_normalize.run(context, {"apps": [pair]}, {})
    drain(result)

    errors = [d for d in result.diagnostics if d.level == "ERROR"]
    assert [e.code for e in errors] == ["NORMALIZE_FAILED"]
    assert errors[0].where == "app-1:BEFORE"
    assert "brand_new_array" in errors[0].message


# ---------------------------------------------------------------------------
# the worker's input check (payload is complete before a single row is read)
# ---------------------------------------------------------------------------


def make_settings(root: Path) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        dataset_root=root,
        script_root=root,
        artifact_root=root / "artifacts",
        compute_python="python3",
    )


def snapshot_of(root_ref: str = "run") -> dict:
    return {
        "root_ref": root_ref,
        "dataset_root_ref": root_ref,
        "slices": {"BEFORE": {"dir": "RESULTS/BEFORE_MASTER"},
                   "AFTER": {"dir": "RESULTS/AFTER_BETA"}},
    }


def test_missing_after_slice_is_a_startup_failure_naming_the_app(tmp_path):
    write_dataset(tmp_path / "run", "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    (tmp_path / "run" / "RESULTS" / "AFTER_BETA" / "app-1.json").unlink()
    job = PipelineJob({"id": 1, "run_id": str(uuid.uuid4())}, make_settings(tmp_path))

    with pytest.raises(StageFailed) as error:
        job._apps(snapshot_of())
    assert "app-1" in str(error.value)
    assert "no AFTER slice" in str(error.value)


def test_missing_before_slice_is_a_startup_failure(tmp_path):
    write_dataset(tmp_path / "run", "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    (tmp_path / "run" / "RESULTS" / "BEFORE_MASTER" / "app-1.json").unlink()
    job = PipelineJob({"id": 1, "run_id": str(uuid.uuid4())}, make_settings(tmp_path))

    with pytest.raises(StageFailed) as error:
        job._apps(snapshot_of())
    assert "app-1" in str(error.value)
    assert "no BEFORE slice" in str(error.value)


def test_empty_dataset_is_an_error_not_an_empty_report(tmp_path):
    (tmp_path / "run" / "RESULTS" / "BEFORE_MASTER").mkdir(parents=True)
    (tmp_path / "run" / "RESULTS" / "AFTER_BETA").mkdir(parents=True)
    job = PipelineJob({"id": 1, "run_id": str(uuid.uuid4())}, make_settings(tmp_path))

    with pytest.raises(StageFailed) as error:
        job._apps(snapshot_of())
    assert "no application pairs" in str(error.value)


def test_dataset_path_escaping_the_root_is_refused(tmp_path):
    write_dataset(tmp_path / "run", "app-1", json.dumps(GOOD_DOC), json.dumps(GOOD_DOC))
    job = PipelineJob({"id": 1, "run_id": str(uuid.uuid4())}, make_settings(tmp_path))

    with pytest.raises(StageFailed) as error:
        job._apps(snapshot_of("../outside"))
    assert "escapes DATASET_ROOT" in str(error.value)
