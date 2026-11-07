"""Worker process entry point.

    python -m change_analysis.worker.main

Loops over the PostgreSQL queue: claim one job, run it, release it.  A watchdog
closes the loop for jobs whose worker died, so nothing stays ``RUNNING`` forever
(PLAN.md §14.1).
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from ..config import load_settings
from ..db.migrations import apply_migrations
from . import queue
from .runner import PipelineJob, run_checks_only, run_llm_job

_STOP = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True
    print(f"[worker] signal {signum}: finishing after the current job", flush=True)


def process(job: dict) -> None:
    settings = load_settings()
    kind = job["kind"]
    if kind == "PIPELINE":
        PipelineJob(job, settings).run()
    elif kind == "CHECKS_ONLY":
        run_checks_only(job, settings)
    elif kind == "LLM":
        run_llm_job(job, settings)
    else:
        queue.finish(int(job["id"]), str(job["run_id"]), "FAILED",
                     error=f"unknown job kind {kind!r}", error_code="UNKNOWN_KIND")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="change-analysis worker")
    parser.add_argument("--once", action="store_true",
                        help="process at most one job and exit (used by tests)")
    parser.add_argument("--idle-exit", type=float, default=0.0,
                        help="exit after N seconds without a job (0 = never)")
    args = parser.parse_args(argv)

    settings = load_settings()
    apply_migrations()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(f"[worker] {queue.WORKER_ID} polling (backend={settings.compute_backend})",
          flush=True)
    idle_since = time.time()
    while not _STOP:
        processed = queue.watchdog(lease_grace_sec=60)
        if processed:
            print(f"[worker] watchdog recovered {processed} expired job(s)", flush=True)
        job = queue.claim_one(lease_sec=settings.lease_sec)
        if job is None:
            if args.once:
                return 0
            if args.idle_exit and time.time() - idle_since > args.idle_exit:
                return 0
            time.sleep(1.0)
            continue
        idle_since = time.time()
        print(f"[worker] job {job['id']} kind={job['kind']} run={job['run_id']}", flush=True)
        try:
            process(job)
        except Exception as exc:  # noqa: BLE001 - a broken job must not kill the loop
            queue.finish(int(job["id"]), str(job["run_id"]), "FAILED",
                         error=f"{type(exc).__name__}: {exc}"[:4000],
                         error_code="WORKER_EXCEPTION", run_state="FAILED")
            print(f"[worker] job {job['id']} crashed: {exc}", flush=True)
        if args.once:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
