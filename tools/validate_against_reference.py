#!/usr/bin/env python3
"""Validation harness — compare our pipeline against the organizers' oracle.

This script is a DEVELOPMENT tool.  It is not imported by the application and
the application never reads the ОРГАНИЗАТОРАМ/ directory (PLAN.md §2.2).

Usage:
    python3 tools/validate_against_reference.py
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from change_analysis.pipeline.core import (  # noqa: E402
    ATTRIBUTE_LOST, ATTRIBUTE_NEW, SAME, VALUE_CHANGED,
    compare_app, normalize_document,
)
from change_analysis.pipeline.segments import build_segment  # noqa: E402

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(APP, "src"))
from change_analysis.config import load_settings  # noqa: E402

#: the organizers' package directory; DATASET_ROOT points at <package>/<somewhere>
#: in the delivered layout, so the parent of DATASET_ROOT is the package for the
#: shipped run.  Override with --package when the layout differs.
PKG = os.environ.get("TASK_PACKAGE") or str(
    os.path.dirname(os.path.dirname(str(load_settings().dataset_root)))
)


def load_run(path: str) -> tuple[list[str], dict[str, dict], dict[str, dict]]:
    manifest = json.load(open(os.path.join(path, "manifest.json"), encoding="utf-8"))
    dirs = {k: os.path.join(path, v["dir"]) for k, v in manifest["srez"].items()}
    files = sorted(os.listdir(dirs["BEFORE"]))
    before, after = {}, {}
    for name in files:
        app_id = os.path.splitext(name)[0]
        before[app_id] = json.load(open(os.path.join(dirs["BEFORE"], name), encoding="utf-8"))
        after[app_id] = json.load(open(os.path.join(dirs["AFTER"], name), encoding="utf-8"))
    return [os.path.splitext(f)[0] for f in files], before, after


def read_expected(csv_path: str) -> dict[str, dict]:
    out = {}
    with open(csv_path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            out[row["APP_ID"]] = row
    return out


def run_pipeline(base_dir: str):
    t0 = time.perf_counter()
    app_ids, before, after = load_run(base_dir)
    t_load = time.perf_counter()
    leaf_counts = []
    comparison = []
    segments = []
    for app_id in app_ids:
        nb = normalize_document(before[app_id], "BEFORE", app_id)
        na = normalize_document(after[app_id], "AFTER", app_id)
        leaf_counts.append((len(nb), len(na)))
        comparison.extend(compare_app(nb, na))
        segments.append(build_segment(app_id, before[app_id], after[app_id]))
    t_pipe = time.perf_counter()
    return {
        "app_ids": app_ids,
        "comparison": comparison,
        "segments": segments,
        "timing": {"load": t_load - t0, "normalize+compare": t_pipe - t_load},
        "leaf_counts": leaf_counts,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for item in sys.argv[1:]:
        if item.startswith("--package="):
            global PKG
            PKG = item.split("=", 1)[1]
    target = (args[0] if args else "OPEN").upper()
    if target == "OPEN":
        base = str(load_settings().dataset_root / "RUN-20260827-0001")
        if not os.path.isdir(base):
            base = os.path.join(PKG, "ЗАДАНИЕ", "data", "RUN-20260827-0001")
        expected_path = os.path.join(PKG, "ОРГАНИЗАТОРАМ", "ЭТАЛОН", "expected_OPEN.csv")
        ref = {"apps": 2002, "changed": 502, "decision_changed": 385,
               "comparison_rows": 289533, "facts": 6784}
    else:
        base = os.path.join(PKG, "ОРГАНИЗАТОРАМ", "HOLDOUT", "RUN-20260827-0001-HOLDOUT")
        expected_path = os.path.join(PKG, "ОРГАНИЗАТОРАМ", "ЭТАЛОН", "expected_HOLDOUT.csv")
        ref = {"apps": 998, "changed": 248, "decision_changed": 186,
               "comparison_rows": None, "facts": None}

    result = run_pipeline(base)
    cmp_rows = result["comparison"]
    segs = result["segments"]

    changed_apps = {r.app_id for r in cmp_rows if r.diff_flag == 1}
    decision_changed = {s["APP_ID"] for s in segs if s["DECISION_CHANGED"]}
    by_type = Counter(r.change_type for r in cmp_rows)

    print(f"=== {target} ===")
    print(f"apps                     {len(result['app_ids']):>8}   expected {ref['apps']}")
    print(f"comparison rows          {len(cmp_rows):>8}   expected {ref['comparison_rows']}")
    print(f"apps with any difference {len(changed_apps):>8}   expected {ref['changed']}")
    print(f"apps with decision flip  {len(decision_changed):>8}   expected {ref['decision_changed']}")
    print("change types            ", dict(by_type))
    lb = [p[0] for p in result["leaf_counts"]]
    la = [p[1] for p in result["leaf_counts"]]
    print(f"leaves/app BEFORE  min={min(lb)} max={max(lb)} median={statistics.median(lb)}")
    print(f"leaves/app AFTER   min={min(la)} max={max(la)} median={statistics.median(la)}")
    print(f"timing load={result['timing']['load']:.2f}s pipe={result['timing']['normalize+compare']:.2f}s")

    if os.path.exists(expected_path):
        expected = read_expected(expected_path)
        seg_by_id = {s["APP_ID"]: s for s in segs}
        fields = ["DECISION_BEFORE", "DECISION_AFTER", "PD_MODEL_BEFORE", "PD_MODEL_AFTER",
                  "CUTOFF_BEFORE", "CUTOFF_AFTER", "SCORE_OKB_BEFORE", "SCORE_OKB_AFTER"]
        mismatches = defaultdict(list)
        for app_id, exp in expected.items():
            seg = seg_by_id.get(app_id)
            if seg is None:
                mismatches["__missing__"].append(app_id)
                continue
            for f in fields:
                if str(seg.get(f, "")) != str(exp.get(f, "")):
                    mismatches[f].append((app_id, seg.get(f), exp.get(f)))
            exp_changed = exp.get("CHANGED") == "1"
            if exp_changed != (app_id in changed_apps):
                mismatches["CHANGED__vs__diff_flag"].append(
                    (app_id, app_id in changed_apps, exp_changed,
                     exp.get("SCENARIO"), exp.get("SUBKIND")))
            exp_dc = exp.get("DECISION_CHANGED") == "1"
            if exp_dc != (app_id in decision_changed):
                mismatches["DECISION_CHANGED"].append((app_id, seg.get("DECISION_CHANGED"), exp.get("DECISION_CHANGED")))
        print("\n--- segment layer vs expected_*.csv ---")
        if not mismatches:
            print("ALL SEGMENT FIELDS MATCH")
        for f, bad in mismatches.items():
            print(f"{f}: {len(bad)} mismatches, e.g. {bad[:6]}")

        # scenario breakdown of the CHANGED mismatch
        if mismatches.get("CHANGED__vs__diff_flag"):
            scen = Counter((b[3], b[4]) for b in mismatches["CHANGED__vs__diff_flag"])
            print("  scenario breakdown of CHANGED mismatch:", dict(scen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
