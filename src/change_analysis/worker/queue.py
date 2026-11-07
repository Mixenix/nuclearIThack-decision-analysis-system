"""PostgreSQL-backed job queue.

Claim in a short transaction with ``FOR UPDATE SKIP LOCKED``, then release the
transaction *before* the long computation (PLAN.md §14.1).  A lease plus a
heartbeat is what distinguishes "still computing" from "process died".
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.connection import tx

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim_one(lease_sec: int = 120) -> dict[str, Any] | None:
    """Atomically take the oldest QUEUED job and turn it into RUNNING."""
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ca.jobs SET
                state = 'RUNNING',
                worker_id = %s,
                lease_until = now() + make_interval(secs => %s),
                attempt = attempt + 1,
                started_at = COALESCE(started_at, now()),
                heartbeat_at = now()
            WHERE id = (
                SELECT id FROM ca.jobs
                WHERE state = 'QUEUED'
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, owner_id, run_id, kind, attempt, payload, state, idempotency_key
            """,
            (WORKER_ID, lease_sec),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE ca.runs SET state = 'RUNNING', started_at = COALESCE(started_at, now())"
            " WHERE id = %s AND state IN ('QUEUED', 'RUNNING')",
            (row["run_id"],),
        )
        return dict(row)


def heartbeat(job_id: int, lease_sec: int = 120) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.jobs SET heartbeat_at = now(),"
            " lease_until = now() + make_interval(secs => %s) WHERE id = %s",
            (lease_sec, job_id),
        )


def finish(job_id: int, run_id: str, state: str, error: str | None = None,
           error_code: str | None = None, run_state: str | None = None) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.jobs SET state = %s, finished_at = now(), error_code = %s,"
            " error_message = %s, lease_until = NULL WHERE id = %s",
            (state, error_code, error, job_id),
        )
        if run_state is not None:
            cur.execute(
                "UPDATE ca.runs SET state = %s, finished_at = CASE WHEN %s IN"
                " ('SUCCEEDED','FAILED','INTERRUPTED') THEN now() ELSE finished_at END,"
                " error_code = COALESCE(%s, error_code),"
                " error_message = COALESCE(%s, error_message) WHERE id = %s",
                (run_state, run_state, error_code, error, run_id),
            )


def watchdog(lease_grace_sec: int = 60) -> list[int]:
    """Mark jobs whose lease expired as INTERRUPTED so they never stay RUNNING forever."""
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.jobs SET state = 'INTERRUPTED', finished_at = now(),"
            " error_code = 'LEASE_EXPIRED',"
            " error_message = 'worker stopped responding (lease expired)'"
            " WHERE state = 'RUNNING'"
            "   AND lease_until IS NOT NULL"
            "   AND lease_until < now() - make_interval(secs => %s)"
            " RETURNING id, run_id",
            (lease_grace_sec,),
        )
        rows = cur.fetchall()
        for row in rows:
            cur.execute(
                "UPDATE ca.runs SET state = 'INTERRUPTED', finished_at = now(),"
                " error_code = 'LEASE_EXPIRED',"
                " error_message = 'worker stopped responding (lease expired)'"
                " WHERE id = %s AND state = 'RUNNING'",
                (row["run_id"],),
            )
        return [int(r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# stage / event helpers used by the runner
# ---------------------------------------------------------------------------


def start_step(run_id: str, job_id: int, stage: str, seq: int,
               procedure_kind: str = "workspace", procedure_ref: str = "",
               procedure_hash: str = "", total: int = 0) -> int:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.run_steps (run_id, job_id, stage, seq, state, progress_total,"
            " procedure_kind, procedure_ref, procedure_hash, started_at)"
            " VALUES (%s, %s, %s, %s, 'RUNNING', %s, %s, %s, %s, now()) RETURNING id",
            (run_id, job_id, stage, seq, total, procedure_kind, procedure_ref,
             procedure_hash),
        )
        step_id = int(cur.fetchone()["id"])
        cur.execute(
            "UPDATE ca.jobs SET current_stage = %s WHERE id = %s", (stage, job_id)
        )
        cur.execute(
            "INSERT INTO ca.run_events (run_id, step_id, level, message)"
            " VALUES (%s, %s, 'INFO', %s)",
            (run_id, step_id, f"stage {stage} started"),
        )
        return step_id


def update_progress(step_id: int, run_id: str, done: int, total: int) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.run_steps SET progress_done = %s, progress_total = %s WHERE id = %s",
            (done, total, step_id),
        )
        cur.execute(
            "UPDATE ca.runs SET processed_pairs = %s WHERE id = %s AND state = 'RUNNING'",
            (done, run_id),
        )


def add_event(run_id: str, step_id: int | None, level: str, message: str) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.run_events (run_id, step_id, level, message) VALUES (%s, %s, %s, %s)",
            (run_id, step_id, level, message[:4000]),
        )


def end_step(step_id: int, state: str, error: str | None = None) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ca.run_steps SET state = %s, error = %s, finished_at = now() WHERE id = %s",
            (state, error, step_id),
        )
