"""Queue behaviour and multi-user isolation — PLAN.md §14, §21.4.

Nothing here touches another agent's data: runs are created by the fixtures with
a ``qa-`` name in the throw-away database and are removed afterwards.
"""

from __future__ import annotations

import json
import uuid

import pytest

from change_analysis.contracts.types import FilterSpec
from change_analysis.db.connection import execute, query, tx
from change_analysis.services import configs, datasets, procedures, report, runs
from change_analysis.services.errors import AccessDenied, InvalidRequest, NotFound
from change_analysis.worker import queue

from qa_support import comparison_row, segment_row

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# queue: a lost worker must not leave a run RUNNING for ever
# ---------------------------------------------------------------------------


def test_watchdog_turns_a_dead_lease_into_an_understandable_state(synthetic_run, owner):
    run_id = synthetic_run(state="RUNNING", rows=[], segments=[])
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.jobs (owner_id, run_id, kind, state, lease_until, started_at)"
            " VALUES (%s,%s,'PIPELINE','RUNNING', now() - interval '10 minutes', now())"
            " RETURNING id", (owner, run_id),
        )
        job_id = int(cur.fetchone()["id"])

    interrupted = queue.watchdog(lease_grace_sec=0)

    assert job_id in interrupted
    job = query("SELECT state, error_code FROM ca.jobs WHERE id = %s", (job_id,))[0]
    assert job["state"] == "INTERRUPTED"
    assert job["error_code"] == "LEASE_EXPIRED"
    run = query("SELECT state, error_code, error_message FROM ca.runs WHERE id = %s",
                (run_id,))[0]
    assert run["state"] == "INTERRUPTED"
    assert run["error_code"] == "LEASE_EXPIRED"
    assert "worker stopped responding" in run["error_message"]


def test_a_retry_is_an_explicit_new_job_on_the_same_run(synthetic_run, owner):
    run_id = synthetic_run(state="FAILED")
    job_id = runs.retry(owner, run_id)
    assert job_id
    run = runs.get_run(owner, run_id)
    assert run["state"] == "QUEUED", "a retry re-queues the same run, it does not copy it"
    assert run["error_code"] is None
    jobs = query("SELECT kind, state FROM ca.jobs WHERE run_id = %s", (run_id,))
    assert len(jobs) == 1 and jobs[0]["kind"] == "PIPELINE" and jobs[0]["state"] == "QUEUED"
    events = runs.run_events(owner, run_id)
    assert any("retry requested" in e["message"] for e in events)


def test_a_succeeded_run_cannot_be_retried(new_run, owner):
    run_id = new_run(name="qa-succeeded")
    execute("UPDATE ca.runs SET state = 'SUCCEEDED' WHERE id = %s", (run_id,))
    with pytest.raises(InvalidRequest):
        runs.retry(owner, run_id)


# ---------------------------------------------------------------------------
# idempotency: the same click must not create a second job
# ---------------------------------------------------------------------------


def test_the_same_idempotency_key_does_not_duplicate_a_run(new_run):
    key = f"qa-idem-{uuid.uuid4()}"
    first = new_run(name="qa-idem", idempotency_key=key)
    second = new_run(name="qa-idem", idempotency_key=key)
    assert first == second
    jobs = query("SELECT id FROM ca.jobs WHERE run_id = %s", (first,))
    assert len(jobs) == 1, "one click, one job"


def test_different_keys_create_different_runs(new_run):
    first = new_run(name="qa-idem-a", idempotency_key=f"qa-{uuid.uuid4()}")
    second = new_run(name="qa-idem-b", idempotency_key=f"qa-{uuid.uuid4()}")
    assert first != second


def test_a_run_is_queued_with_its_full_snapshot(new_run, owner):
    run_id = new_run(name="qa-snapshot")
    run = runs.get_run(owner, run_id)
    assert run["state"] == "QUEUED"
    snapshot = run["snapshot"]
    for stage in ("normalize", "compare", "aggregate"):
        entry = snapshot["procedures"][stage]
        assert entry["code"].strip(), f"{stage} has no frozen code"
        assert entry["source_kind"] in ("file", "workspace")
    assert snapshot["checks"] == []
    assert snapshot["slices"]["BEFORE"]["dir"]
    assert run["expected_pairs"] > 0


def test_a_procedure_can_be_replaced_through_configuration_only(new_run, owner,
                                                                dataset_id):
    """Приёмка: «может заменить выбранный обработчик без изменения кода приложения»."""
    code = "# qa workspace aggregate\ndef run(context, inputs, params):\n    raise SystemExit('qa')\n"
    procedure_id = procedures.save_procedure(
        owner, "aggregate", "QA aggregate из рабочей области", "workspace", code=code)
    try:
        selections = {
            "procedures": {
                "normalize": procedures.default_selection("normalize"),
                "compare": procedures.default_selection("compare"),
                "aggregate": {"source": "workspace", "procedure_id": procedure_id,
                              "params": {"dimensions": ["PATH"]}},
            },
        }
        run_id = new_run(name="qa-workspace-procedure", selections=selections)
        snapshot = runs.get_run(owner, run_id)["snapshot"]
        aggregate = snapshot["procedures"]["aggregate"]
        assert aggregate["source_kind"] == "workspace"
        assert aggregate["source_ref"] == procedure_id
        assert aggregate["code"] == code, "the workspace text is what will execute"
        assert aggregate["params"] == {"dimensions": ["PATH"]}
        # the other two stages still come from the registered files
        assert snapshot["procedures"]["normalize"]["source_kind"] == "file"
        assert snapshot["procedures"]["normalize"]["code"].strip()

        # the frozen copy on the run is what the stage log points at
        rows = query("SELECT stage, source_kind, source_ref, code_snapshot"
                     " FROM ca.run_procedures WHERE run_id = %s ORDER BY stage", (run_id,))
        by_stage = {row["stage"]: row for row in rows}
        assert by_stage["aggregate"]["source_kind"] == "workspace"
        assert by_stage["aggregate"]["code_snapshot"] == code
    finally:
        procedures.delete_procedure(owner, procedure_id)


# ---------------------------------------------------------------------------
# ownership: B must not read A's run, report, checks or export
# ---------------------------------------------------------------------------


def test_a_second_user_cannot_read_another_users_run(synthetic_run, owner, other_owner):
    run_id = synthetic_run(rows=[comparison_row("x", "app-1", "p.q",
                                                change_type="VALUE_CHANGED")],
                           segments=[segment_row("x", "app-1")])

    assert runs.get_run(owner, run_id)["id"] == run_id
    for call in (
        lambda: runs.get_run(other_owner, run_id),
        lambda: runs.status_summary(other_owner, run_id),
        lambda: runs.run_stages(other_owner, run_id),
        lambda: runs.run_events(other_owner, run_id),
        lambda: runs.request_checks_only(other_owner, run_id, []),
        lambda: report.summary(other_owner, run_id, FilterSpec()),
        lambda: report.pivot(other_owner, run_id, ["CLASS"], [], FilterSpec()),
        lambda: report.apps_page(other_owner, run_id, FilterSpec()),
        lambda: report.app_detail(other_owner, run_id, "app-1"),
        lambda: report.attribute_rows(other_owner, run_id, "app-1"),
        lambda: report.raw_json(other_owner, run_id, "app-1", "BEFORE"),
        lambda: report.export_html(other_owner, run_id),
    ):
        with pytest.raises((AccessDenied, NotFound)):
            call()


def test_check_report_is_owner_checked(synthetic_run, other_owner):
    from change_analysis.services import checks as checks_service

    run_id = synthetic_run(rows=[comparison_row("x", "app-1", "p.q")],
                           segments=[segment_row("x", "app-1")])
    with pytest.raises((AccessDenied, NotFound)):
        checks_service.check_report(other_owner, run_id)


def test_export_files_are_per_run_and_do_not_leak_between_users(synthetic_run, owner,
                                                                other_owner):
    """Two runs with distinct applications: neither export may contain the other.

    Each run publishes one check row that names its application, which is exactly
    how application ids reach the document.
    """
    from qa_support import insert_check_result

    run_a = synthetic_run(name="qa-export-a",
                          rows=[comparison_row("x", "app-A", "p.q",
                                               change_type="VALUE_CHANGED")],
                          segments=[segment_row("x", "app-A")])
    run_b = synthetic_run(name="qa-export-b",
                          rows=[comparison_row("x", "app-B", "p.q",
                                               change_type="VALUE_CHANGED")],
                          segments=[segment_row("x", "app-B")])
    for run_id, app in ((run_a, "app-A"), (run_b, "app-B")):
        insert_check_result(tx, run_id, 1, "qa_leak_probe", "QA: проба изоляции",
                            rows=[{"LABEL": "QA: проба изоляции", "SOURCE": "AFTER",
                                   "ARGUMENT": app, "CNT": 1, "TOTAL": 1,
                                   "METRIC": 1.0, "APP_IDS": [app],
                                   "RESULT": "FAIL", "CONFIDENCE": "LOW"}])

    path_a = report.export_html(owner, run_a)
    path_b = report.export_html(owner, run_b)
    assert path_a != path_b
    html_a = open(path_a, encoding="utf-8").read()
    html_b = open(path_b, encoding="utf-8").read()
    assert "qa-export-a" in html_a and "qa-export-b" not in html_a
    assert "app-A" in html_a and "app-B" not in html_a
    assert "app-B" in html_b and "app-A" not in html_b


def test_procedures_are_owned(owner, other_owner):
    procedure_id = procedures.save_procedure(
        owner, "check", "qa-private-check", "workspace",
        code="def run(mart, params=None):\n    return []\nCHECK = {'id': 'x',"
             " 'label': 'x', 'params': {}, 'entrypoint': run}\n",
    )
    try:
        assert procedures.get_procedure(owner, procedure_id)["name"] == "qa-private-check"
        with pytest.raises(NotFound):
            procedures.get_procedure(other_owner, procedure_id)
        with pytest.raises(NotFound):
            procedures.save_procedure(other_owner, "check", "hijack", "workspace",
                                      code="x = 1", procedure_id=procedure_id)
    finally:
        procedures.delete_procedure(owner, procedure_id)


def test_configurations_are_owned_and_listed_separately(dataset_id, owner, other_owner):
    config_a = configs.save_configuration(
        owner, "qa-config-a", dataset_id, configs.default_selections(owner))
    config_b = configs.save_configuration(
        other_owner, "qa-config-b", dataset_id, configs.default_selections(other_owner))
    try:
        names_a = {c["name"] for c in configs.list_configurations(owner)}
        names_b = {c["name"] for c in configs.list_configurations(other_owner)}
        assert "qa-config-a" in names_a and "qa-config-b" not in names_a
        assert "qa-config-b" in names_b and "qa-config-a" not in names_b
        assert configs.load_configuration(owner, config_a)["name"] == "qa-config-a"
        with pytest.raises(NotFound):
            configs.load_configuration(other_owner, config_a)
        assert config_a != config_b
    finally:
        configs.delete_configuration(owner, config_a)
        configs.delete_configuration(other_owner, config_b)


def test_a_private_dataset_is_invisible_to_another_user(owner, other_owner):
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.datasets (owner_id, name, root_ref, shared)"
            " VALUES (%s,'qa-private-dataset',%s,false) RETURNING id",
            (owner, f"qa-private-{uuid.uuid4()}"),
        )
        dataset_id = str(cur.fetchone()["id"])
    try:
        assert datasets.get_dataset(owner, dataset_id)["name"] == "qa-private-dataset"
        with pytest.raises(NotFound):
            datasets.get_dataset(other_owner, dataset_id)
        visible = {d["id"] for d in datasets.list_datasets(other_owner)}
        assert dataset_id not in visible
    finally:
        execute("DELETE FROM ca.datasets WHERE id = %s", (dataset_id,))


# ---------------------------------------------------------------------------
# frozen code: a later edit must not silently change what a run executed
# ---------------------------------------------------------------------------


def test_edited_procedure_is_flagged_as_code_changed_since_the_run(synthetic_run, owner):
    run_id = synthetic_run(state="SUCCEEDED")
    code = "print('new code')"
    execute("UPDATE ca.runs SET snapshot = %s::jsonb WHERE id = %s",
            (json.dumps({"procedures": {"normalize": {"code": code}}}), run_id))
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.run_steps (run_id, stage, seq, state, procedure_kind,"
            " procedure_ref, procedure_hash) VALUES (%s,'normalize',1,'DONE','file',"
            " 'spr_normalize.py','0000000000000000')", (run_id,),
        )
    stages = runs.run_stages(owner, run_id)
    assert len(stages) == 1
    assert stages[0]["code_changed_since_run"] is True


def test_matching_hash_is_not_flagged(synthetic_run, owner):
    import hashlib

    code = "print('unchanged')"
    run_id = synthetic_run(state="SUCCEEDED")
    execute("UPDATE ca.runs SET snapshot = %s::jsonb WHERE id = %s",
            (json.dumps({"procedures": {"normalize": {"code": code}}}), run_id))
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.run_steps (run_id, stage, seq, state, procedure_kind,"
            " procedure_ref, procedure_hash) VALUES (%s,'normalize',1,'DONE','file',"
            " 'spr_normalize.py',%s)",
            (run_id, hashlib.sha256(code.encode()).hexdigest()[:16]),
        )
    stages = runs.run_stages(owner, run_id)
    assert stages[0]["code_changed_since_run"] is False
