"""Exercise normalize artifacts and parent failure/event handling together."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from change_analysis.config import Settings
from change_analysis.procedures import spr_normalize
from change_analysis.worker import queue
from change_analysis.worker.runner import PipelineJob, StageFailed

pytestmark = pytest.mark.unit


@pytest.fixture
def batch(tmp_path, monkeypatch):
    settings = Settings(
        database_url="postgresql://unused/unused", dataset_root=tmp_path,
        script_root=tmp_path, artifact_root=tmp_path / "artifacts",
        compute_python=sys.executable,
    )
    job = PipelineJob({"id": 1, "run_id": str(uuid.uuid4())}, settings)
    snapshot = {"slices": {"BEFORE": {"dir": "before"}, "AFTER": {"dir": "after"}}}
    for app_id in ("app-1", "app-2"):
        doc = {"appData": {"application_id": app_id},
               "decisionResult": {"decision": "APPROVE"}}
        for source in ("before", "after"):
            directory = tmp_path / source
            directory.mkdir(exist_ok=True)
            (directory / f"{app_id}.json").write_text(json.dumps(doc), encoding="utf-8")

    events, steps = [], []
    monkeypatch.setattr(queue, "start_step", lambda *a, **kw: 123)
    monkeypatch.setattr(queue, "update_progress", lambda *a, **kw: None)
    monkeypatch.setattr(queue, "heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(queue, "end_step", lambda *args: steps.append(args))
    monkeypatch.setattr(queue, "add_event", lambda *args: events.append(args))

    def run(params=None):
        apps = job._apps(snapshot, require_complete=False)
        return job._run_stage(
            "normalize", Path(spr_normalize.__file__).read_text(encoding="utf-8"),
            params or {}, {"apps": apps}, "workspace", "", len(apps),
        )

    return job, run, events, steps


@pytest.mark.parametrize("source", ["before", "after"])
def test_bad_json_fails_stage_with_app_id_file_and_event(tmp_path, batch, source):
    job, run, events, steps = batch
    broken = tmp_path / source / "app-2.json"
    broken.write_text("{broken", encoding="utf-8")

    with pytest.raises(StageFailed) as error:
        run({"max_errors_reported": 0})

    assert "app-2" in str(error.value)
    assert str(broken) in str(error.value)
    assert steps[-1][1] == "FAILED"
    result = json.loads((job.root / "normalize/result.json").read_text())
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == "BAD_JSON"
    assert any(level == "ERROR" and "BAD_JSON app-2" in message
               and str(broken) in message for _, _, level, message in events)


@pytest.mark.parametrize("source", ["before", "after"])
def test_unpaired_application_fails_normalize(tmp_path, batch, source):
    job, run, events, steps = batch
    missing = tmp_path / source / "app-2.json"
    missing.unlink()

    with pytest.raises(StageFailed) as error:
        run()

    assert error.value.stage == "normalize"
    assert "app-2" in str(error.value)
    assert str(missing) in str(error.value)
    assert steps[-1][1] == "FAILED"
    result = json.loads((job.root / "normalize/result.json").read_text())
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == "INCOMPLETE_PAIR"
    assert any("INCOMPLETE_PAIR app-2" in e[3] for e in events)


def test_two_valid_pairs_succeed_without_diagnostics(batch):
    job, run, events, steps = batch
    outcome = run()

    assert steps[-1][1] == "DONE"
    result = json.loads((outcome.out_dir / "result.json").read_text())
    assert result["ok"] is True
    assert result["diagnostics"] == []
    assert result["counts"]["segments"] == 2
    with (outcome.out_dir / "rows.jsonl").open() as rows:
        assert {json.loads(row)["app_id"] for row in rows} == {"app-1", "app-2"}
    assert all(event[2] == "INFO" for event in events)


def test_identity_warning_is_logged_without_failing_batch(tmp_path, batch):
    job, run, events, steps = batch
    path = tmp_path / "before/app-2.json"
    path.write_text(json.dumps({"appData": {"application_id": "other"}}))

    outcome = run()

    result = json.loads((outcome.out_dir / "result.json").read_text())
    assert result["ok"] is True
    assert result["diagnostics"][0]["code"] == "APP_ID_MISMATCH"
    assert any(e[2] == "WARNING" and "app-2" in e[3] for e in events)
