"""Runs: creation, queueing, status, isolation.

Creating a run freezes an immutable snapshot of everything that will execute —
dataset slice paths, the *code text* of the three procedures, the check code and
their parameters — so a later edit can never change what a started run did
(PLAN.md §11.4).  A run is never shared between users: every accessor takes
``owner_id`` and raises :class:`AccessDenied` otherwise.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Sequence

from ..config import load_settings
from ..db.connection import query, tx
from . import datasets, procedures
from .errors import AccessDenied, InvalidRequest, NotFound

TERMINAL = ("SUCCEEDED", "FAILED", "INTERRUPTED")


def _owned_run(owner_id: str, run_id: str) -> dict[str, Any]:
    rows = query("SELECT * FROM ca.runs WHERE id = %s", (run_id,))
    if not rows:
        raise NotFound("run not found")
    if str(rows[0]["owner_id"]) != str(owner_id):
        raise AccessDenied("run")
    return rows[0]


def create_run(
    owner_id: str,
    name: str,
    dataset_id: str,
    selections: dict[str, Any],
    params: dict[str, Any] | None = None,
    backend: str = "cpu",
    idempotency_key: str | None = None,
) -> str:
    dataset = datasets.get_dataset(owner_id, dataset_id)
    check = datasets.precheck_dataset(dataset["root_ref"], dataset.get("abs_root"),
                                      dataset.get("file_glob", datasets.DEFAULT_FILE_GLOB))
    if not check["ok"]:
        raise InvalidRequest("dataset precheck failed: " + "; ".join(check["errors"]))
    if backend not in ("cpu", "gpu"):
        raise InvalidRequest(f"unknown backend {backend!r}")
    if backend == "gpu" and not load_settings().compute_backend == "gpu":
        # never pretend to run on a GPU that is not configured
        raise InvalidRequest(
            "GPU backend is not configured on this installation "
            "(COMPUTE_BACKEND=cpu); the run would silently fall back to pandas"
        )

    procedures_snapshot: dict[str, Any] = {}
    for stage in procedures.PROCEDURE_STAGES:
        resolved = procedures.resolve_procedure(
            owner_id, stage, (selections.get("procedures") or {}).get(stage)
        )
        if stage == "normalize" and resolved["params"].get("reader_ref"):
            resolved["params"] = dict(resolved["params"])
            ref = resolved["params"]["reader_ref"]
            if not ref.endswith(".py"):
                raise InvalidRequest("reader_ref must be a .py file in SCRIPT_ROOT")
            resolved["params"]["reader_code"] = procedures.read_registered_file(ref)
        procedures_snapshot[stage] = {
            "code": resolved["code"],
            "source_kind": resolved["source_kind"],
            "source_ref": resolved["source_ref"],
            "params": resolved["params"],
        }

    check_snapshot = procedures.resolve_checks(owner_id, selections.get("checks"))
    manifest = dataset.get("manifest") or {}
    slices = manifest.get("srez") or {}
    snapshot = {
        "root_ref": dataset["root_ref"],
        "dataset_root_ref": dataset["root_ref"],
        "abs_root": dataset.get("abs_root") or "",
        "file_glob": dataset.get("file_glob", datasets.DEFAULT_FILE_GLOB),
        "slices": {
            "BEFORE": {"dir": (slices.get("BEFORE") or {}).get("dir"),
                       "label": (slices.get("BEFORE") or {}).get("label", "MASTER")},
            "AFTER": {"dir": (slices.get("AFTER") or {}).get("dir"),
                      "label": (slices.get("AFTER") or {}).get("label", "BETA")},
        },
        "procedures": procedures_snapshot,
        "checks": check_snapshot,
        "params": params or {},
        "backend": backend,
        "technical_fields": manifest.get("technical_fields_always_differ") or [],
    }

    key = idempotency_key or str(uuid.uuid4())
    with tx() as conn, conn.cursor() as cur:
        cur.execute("SELECT run_id FROM ca.jobs WHERE idempotency_key = %s", (key,))
        existing = cur.fetchone()
        if existing:
            # the same click (same idempotency token) must not create a second job
            return str(existing["run_id"])
        cur.execute(
            "INSERT INTO ca.runs (owner_id, dataset_id, config_id, name, snapshot,"
            " state, backend, expected_pairs)"
            " VALUES (%s,%s,%s,%s,%s::jsonb,'QUEUED',%s,%s) RETURNING id",
            (owner_id, dataset_id, selections.get("config_id"), name,
             json.dumps(snapshot), backend, check["pairs"]),
        )
        run_id = str(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO ca.jobs (owner_id, run_id, kind, state, idempotency_key)"
            " VALUES (%s,%s,'PIPELINE','QUEUED',%s)",
            (owner_id, run_id, key),
        )
        cur.execute(
            "INSERT INTO ca.run_events (run_id, level, message) VALUES (%s,'INFO',%s)",
            (run_id, f"run created: {check['pairs']} application pairs queued"),
        )
        for stage, spec in procedures_snapshot.items():
            cur.execute(
                "INSERT INTO ca.run_procedures (run_id, stage, source_kind, source_ref,"
                " procedure_id, code_snapshot, params)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (run_id, stage) DO NOTHING",
                (run_id, stage, spec["source_kind"], spec["source_ref"],
                 spec["source_ref"] if spec["source_kind"] == "workspace" else None,
                 spec["code"], json.dumps(spec["params"])),
            )
    return run_id


def _published_check_set(run_id: str) -> list[dict[str, Any]]:
    """The check specifications of the newest published report revision.

    The published revision, not the run snapshot, is the authority on what has to
    be republished — see migrations/0005.
    """
    rows = query(
        "SELECT DISTINCT ON (check_id) check_id, spec FROM ca.check_executions"
        " WHERE run_id = %s AND state <> 'RUNNING'"
        " ORDER BY check_id, revision DESC", (run_id,))
    out: list[dict[str, Any]] = []
    for row in rows:
        spec = row.get("spec")
        if not spec:
            continue
        out.append({"id": spec.get("id") or row["check_id"],
                    "label": spec.get("label", ""),
                    "code": spec.get("code", ""),
                    "params": spec.get("params") or {},
                    "source_kind": spec.get("source_kind", "workspace"),
                    "source_ref": spec.get("source_ref", "")})
    return out


def list_runs(owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = query(
        "SELECT id, name, state, backend, expected_pairs, processed_pairs, summary,"
        " created_at, started_at, finished_at, error_message, revision"
        " FROM ca.runs WHERE owner_id = %s ORDER BY created_at DESC LIMIT %s",
        (owner_id, limit),
    )
    return [{**r, "id": str(r["id"])} for r in rows]


def get_run(owner_id: str, run_id: str) -> dict[str, Any]:
    run = _owned_run(owner_id, run_id)
    out = {k: (str(v) if k in ("id", "owner_id", "dataset_id", "config_id") else v)
           for k, v in run.items()}
    out["dataset_name"] = ""
    if run["dataset_id"]:
        rows = query("SELECT name FROM ca.datasets WHERE id = %s", (run["dataset_id"],))
        if rows:
            out["dataset_name"] = rows[0]["name"]
    return out


def run_stages(owner_id: str, run_id: str) -> list[dict[str, Any]]:
    _owned_run(owner_id, run_id)
    rows = query(
        "SELECT id, stage, seq, state, progress_done, progress_total, procedure_kind,"
        " procedure_ref, procedure_hash, detail, error, started_at, finished_at"
        " FROM ca.run_steps WHERE run_id = %s ORDER BY id", (run_id,),
    )
    snapshot = _owned_run(owner_id, run_id)["snapshot"] or {}
    current_code = {
        stage: (snapshot.get("procedures") or {}).get(stage, {}).get("code", "")
        for stage in ("normalize", "compare", "aggregate")
    }
    import hashlib

    for row in rows:
        stage = row["stage"]
        expected = current_code.get(stage, "")
        if expected and row["procedure_hash"]:
            actual_hash = hashlib.sha256(expected.encode()).hexdigest()[:16]
            row["code_changed_since_run"] = actual_hash != row["procedure_hash"]
        else:
            row["code_changed_since_run"] = False
    return rows


def run_events(owner_id: str, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    _owned_run(owner_id, run_id)
    return query(
        "SELECT id, step_id, level, message, created_at FROM ca.run_events"
        " WHERE run_id = %s AND id > %s ORDER BY id LIMIT %s",
        (run_id, int(after_id), int(limit)),
    )


def status_summary(owner_id: str, run_id: str) -> dict[str, Any]:
    run = _owned_run(owner_id, run_id)
    steps = query(
        "SELECT stage, state, progress_done, progress_total, started_at, finished_at, error"
        " FROM ca.run_steps WHERE run_id = %s ORDER BY id", (run_id,),
    )
    return {
        "id": str(run["id"]),
        "name": run["name"],
        "state": run["state"],
        "backend": run["backend"],
        "expected_pairs": run["expected_pairs"],
        "processed_pairs": run["processed_pairs"],
        "summary": run["summary"],
        "error_message": run["error_message"],
        "revision": run["revision"],
        "finished": run["state"] in TERMINAL,
        "steps": steps,
    }


def request_checks_only(
    owner_id: str,
    run_id: str,
    check_selections: Sequence[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> str:
    """Run extra checks on an already finished run without recomputing it."""
    run = _owned_run(owner_id, run_id)
    if run["state"] != "SUCCEEDED":
        raise InvalidRequest("checks can only be re-run on a finished run")
    snapshot = dict(run["snapshot"] or {})
    added = procedures.resolve_checks(owner_id, list(check_selections))
    existing = {c["id"]: c for c in _published_check_set(run_id)}
    if not existing:
        # nothing published yet (or an old run): fall back to the snapshot
        existing = {c["id"]: c for c in (snapshot.get("checks") or [])}
    for spec in added:
        existing[spec["id"]] = spec          # same id replaces, never duplicates
    snapshot["checks"] = list(existing.values())
    snapshot["checks_only"] = added
    new_revision = int(run["revision"]) + 1
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.runs SET snapshot = %s::jsonb, revision = %s WHERE id = %s",
            (json.dumps(snapshot), new_revision, run_id),
        )
        cur.execute(
            "INSERT INTO ca.jobs (owner_id, run_id, kind, state, payload)"
            " VALUES (%s,%s,'CHECKS_ONLY','QUEUED',%s::jsonb) RETURNING id",
            (owner_id, run_id, json.dumps({"checks": [c["id"] for c in added]})),
        )
        job_id = str(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO ca.run_events (run_id, level, message) VALUES (%s,'INFO',%s)",
            (run_id, f"checks-only job queued: {', '.join(c['id'] for c in added)}"),
        )
    return job_id


def retry(owner_id: str, run_id: str) -> str:
    """An explicit retry is a new job on the same run — never automatic."""
    run = _owned_run(owner_id, run_id)
    if run["state"] not in ("FAILED", "INTERRUPTED"):
        raise InvalidRequest("only failed or interrupted runs can be retried")
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.jobs (owner_id, run_id, kind, state) VALUES (%s,%s,'PIPELINE','QUEUED')"
            " RETURNING id", (owner_id, run_id),
        )
        job_id = str(cur.fetchone()["id"])
        cur.execute(
            "UPDATE ca.runs SET state = 'QUEUED', error_code = NULL, error_message = NULL,"
            " finished_at = NULL, processed_pairs = 0 WHERE id = %s", (run_id,),
        )
        cur.execute(
            "INSERT INTO ca.run_events (run_id, level, message) VALUES (%s,'INFO',%s)",
            (run_id, f"retry requested (job {job_id})"),
        )
    return job_id
