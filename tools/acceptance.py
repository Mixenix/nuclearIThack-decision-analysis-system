#!/usr/bin/env python3
"""Acceptance check: run the whole application on a dataset and compare the two
published indicator numbers with the organizers' reference.

This script is a *verification harness*, not part of the application: the app
never reads the reference answers.  It exists so that "our number matches theirs"
is a reproducible command instead of a claim.

    # the 998 held-out applications (the number the judges will look at)
    python tools/acceptance.py --run-dir <dir with manifest.json> \
        --expected <expected_HOLDOUT.csv> --expect-changed 248 --expect-flips 186
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))

from change_analysis.auth import service as auth  # noqa: E402
from change_analysis.config import load_settings  # noqa: E402
from change_analysis.db.connection import query  # noqa: E402
from change_analysis.db.migrations import apply_migrations  # noqa: E402
from change_analysis.services import configs, datasets, runs  # noqa: E402
from change_analysis.worker.main import main as worker_main  # noqa: E402


def read_expected(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig") as fh:
        return {row["APP_ID"]: row for row in csv.DictReader(fh, delimiter=";")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True,
                        help="directory containing manifest.json")
    parser.add_argument("--expected", help="expected_*.csv from the organizers")
    parser.add_argument("--expect-changed", type=int)
    parser.add_argument("--expect-flips", type=int)
    parser.add_argument("--dataset-root",
                        help="override DATASET_ROOT for this check only (the run "
                             "directory must live inside it)")
    parser.add_argument("--login", default="analyst")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    if args.dataset_root:
        os.environ["DATASET_ROOT"] = str(Path(args.dataset_root).resolve())
        from change_analysis.config import load_settings as _reload

        _reload(force=True)

    apply_migrations()
    settings = load_settings()
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "manifest.json").is_file():
        print(f"no manifest.json in {run_dir}", file=sys.stderr)
        return 2

    # The dataset must live inside DATASET_ROOT: the application refuses to read
    # outside it, and silently widening the root at runtime would be a lie.
    root = settings.dataset_root.resolve()
    if not run_dir.is_relative_to(root):
        print(f"{run_dir} is outside DATASET_ROOT ({root}).\n"
              f"Set DATASET_ROOT to a common parent in app/.env first.", file=sys.stderr)
        return 2
    root_ref = str(run_dir.relative_to(root))

    owner = auth.ensure_user(args.login, args.login, args.login)
    dataset_id = datasets.register_dataset(owner, f"acceptance {run_dir.name}", root_ref)
    selections = configs.default_selections(owner)
    selections["checks"] = [
        {"id": cid, "params": {}} for cid in (
            "attribute_new_loss", "stop_factor_disappeared", "target_model_changed",
            "external_score_drift", "ar_drop_by_product",
        )
    ]

    started = time.perf_counter()
    run_id = runs.create_run(owner, args.name or f"acceptance {run_dir.name}",
                             dataset_id, selections, backend="cpu")
    print(f"run {run_id} queued, executing...")
    worker_main(["--once"])
    elapsed = time.perf_counter() - started

    run = runs.get_run(owner, run_id)
    summary = run["summary"] or {}
    print(f"state={run['state']} elapsed={elapsed:.1f}s")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if run["state"] != "SUCCEEDED":
        print("FAILED:", run.get("error_message"))
        for event in runs.run_events(owner, run_id, limit=30):
            print(f"  [{event['level']}] {event['message']}")
        return 1

    changed = int(summary.get("changed") or 0)
    flips = int(summary.get("decision_changed") or 0)
    ok = True
    if args.expect_changed is not None and changed != args.expect_changed:
        print(f"MISMATCH changed: got {changed}, expected {args.expect_changed}")
        ok = False
    if args.expect_flips is not None and flips != args.expect_flips:
        print(f"MISMATCH decision flips: got {flips}, expected {args.expect_flips}")
        ok = False

    if args.expected:
        expected = read_expected(Path(args.expected))
        segments = {row["app_id"]: row for row in query(
            "SELECT * FROM ca.app_segments WHERE run_id = %s", (run_id,))}
        mismatched: list[tuple[str, str, object, object]] = []
        for app_id, exp in expected.items():
            seg = segments.get(app_id)
            if seg is None:
                mismatched.append((app_id, "MISSING", None, None))
                continue
            for column, field in (
                ("DECISION_BEFORE", "decision_before"), ("DECISION_AFTER", "decision_after"),
                ("PD_MODEL_BEFORE", "pd_model_before"), ("PD_MODEL_AFTER", "pd_model_after"),
                ("CUTOFF_BEFORE", "cutoff_before"), ("CUTOFF_AFTER", "cutoff_after"),
                ("SCORE_OKB_BEFORE", "score_okb_before"),
                ("SCORE_OKB_AFTER", "score_okb_after"),
            ):
                if str(seg[field]) != str(exp.get(column, "")):
                    mismatched.append((app_id, column, seg[field], exp.get(column)))
        print(f"segment fields compared on {len(expected)} applications: "
              f"{len(mismatched)} mismatches")
        if mismatched:
            ok = False
            for item in mismatched[:10]:
                print("   ", item)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
