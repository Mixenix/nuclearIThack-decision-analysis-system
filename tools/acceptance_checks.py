#!/usr/bin/env python3
"""Acceptance run: drives the same code path the interface drives and checks numbers.

Путь тот же, что у интерфейса: вход → датасет → выбор процедур и проверок →
создание прогона → воркер → отчёт → сводная → переход к заявке → экспорт HTML.
Скрипт печатает машиночитаемые проверки и возвращает код 1, если хоть одна упала.

    .venv/bin/python tools/acceptance_checks.py                 # переиспользовать демо-прогон
    .venv/bin/python tools/acceptance_checks.py --run-id <id>   # конкретный прогон
    .venv/bin/python tools/acceptance_checks.py --fresh         # создать новый прогон (≈90 с)

Это проверочный инструмент, не часть приложения: приложение никогда не читает
эталонные ответы.
"""

from __future__ import annotations

import json
import sys
import time
import traceback

from change_analysis.auth import service as auth
from change_analysis.config import load_settings
from change_analysis.contracts.types import FilterSpec
from change_analysis.db.connection import query
from change_analysis.services import checks as checks_service
from change_analysis.services import configs, datasets, report, runs

RESULTS: list[tuple[str, bool, str]] = []


def _args():
    import argparse

    parser = argparse.ArgumentParser(description="Приёмка: сквозная проверка на демо-прогоне")
    parser.add_argument("--run-id", default="", help="готовый прогон (по умолчанию — последний)")
    parser.add_argument("--fresh", action="store_true", help="создать новый прогон вместо готового")
    parser.add_argument("--user", default="analyst")
    parser.add_argument("--password", default="analyst")
    return parser.parse_args()


ARGS = _args()


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    s = load_settings()
    print(f"dsn={s.database_url}\nbase_url={s.public_base_url} backend={s.compute_backend}")
    print(f"llm_available={s.llm_available()} provider={s.llm_provider} model={s.llm_model}")

    # --- 1. authentication (what /login does server-side) -------------------
    uid = auth.authenticate(ARGS.user, ARGS.password)
    check("login analyst", uid is not None)
    uid2 = auth.authenticate("analyst2", "analyst2")
    check("login analyst2", uid2 is not None)
    check("wrong password rejected", auth.authenticate("analyst", "nope") is None)
    if not uid:
        return 1

    # --- 2. dataset registration + precheck --------------------------------
    ds_all = datasets.list_datasets(uid)
    ok_ds = []
    for d in ds_all:
        pcheck = datasets.precheck_dataset(d["root_ref"])
        print(f"  dataset {d['name']!r} root_ref={d['root_ref']!r} "
              f"ok={pcheck.get('ok')} pairs={pcheck.get('pairs')}")
        if pcheck.get("ok") and pcheck.get("pairs") == 2002:
            ok_ds.append(d)
    check("dataset registered", bool(ok_ds), f"{len(ok_ds)} usable of {len(ds_all)}")
    if not ok_ds:
        return 1
    ds = ok_ds
    pre = datasets.precheck_dataset(ds[0]["root_ref"])
    check(
        "precheck pairs == 2002",
        pre.get("pairs") == 2002,
        json.dumps({k: pre.get(k) for k in ("pairs", "before", "after")}, ensure_ascii=False),
    )
    check("precheck has no errors", not pre.get("errors"), str(pre.get("errors"))[:200])

    # --- 3. configuration + run (the UI "Запустить" path) ------------------
    selections = configs.default_selections(uid)
    selections["checks"] = [
        {"id": cid, "params": {}}
        for cid in (
            "attribute_new_loss",
            "stop_factor_disappeared",
            "target_model_changed",
            "external_score_drift",
            "ar_drop_by_product",
        )
    ]
    preexisting = ARGS.run_id
    if not preexisting and not ARGS.fresh:
        from change_analysis.db.connection import query as _q
        rows = _q("SELECT id FROM ca.runs WHERE owner_id = %s AND state = 'SUCCEEDED'"
                  " ORDER BY created_at DESC LIMIT 1", (uid,))
        preexisting = str(rows[0]["id"]) if rows else ""
    if preexisting:
        run_id = preexisting
        r = runs.get_run(uid, run_id)
        print(f"using existing run {run_id} state={r['state']}")
        check("pre-existing run SUCCEEDED", r["state"] == "SUCCEEDED", r["state"])
        t0 = time.perf_counter()
        wall = 0.0
        _skip_create = True
    else:
        _skip_create = False
    t0 = time.perf_counter()
    if not _skip_create:
        run_id = runs.create_run(uid, "Приёмка: полный прогон", ds[0]["id"], selections, backend="cpu")
        print(f"run_id={run_id} queued in {time.perf_counter() - t0:.2f}s")

    # idempotency: the same token must not create a second job
    if not _skip_create:
        token_run = runs.create_run(
            uid, "Приёмка: полный прогон", ds[0]["id"], selections, backend="cpu",
            idempotency_key="acceptance-token-1",
        )
        token_run2 = runs.create_run(
            uid, "Приёмка: полный прогон", ds[0]["id"], selections, backend="cpu",
            idempotency_key="acceptance-token-1",
        )
        check("idempotency token reuses the run", token_run == token_run2, token_run)

    # --- 4. wait for the worker -------------------------------------------
    deadline = time.time() + 900
    state = "SUCCEEDED" if _skip_create else "QUEUED"
    last = ""
    while not _skip_create and time.time() < deadline:
        r = runs.get_run(uid, run_id)
        state = r["state"]
        if state != last:
            print(f"  state -> {state}")
            last = state
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    wall = wall if _skip_create else time.perf_counter() - t0
    check("run SUCCEEDED", state == "SUCCEEDED", f"state={state} wall={wall:.1f}s")
    if state != "SUCCEEDED":
        for ev in runs.run_events(uid, run_id, limit=40):
            print(f"  [{ev['level']}] {ev['message']}")
        return 1

    run = runs.get_run(uid, run_id)
    total = runs.run_stages(uid, run_id)
    check("stage log has all stages", len(total) >= 3,
          ", ".join(f"{st['stage']}={st['state']}" for st in total))

    # --- 5. the reference numbers -----------------------------------------
    row = query(
        "SELECT count(*) AS n FROM ca.comparison_rows WHERE run_id = %s", (run_id,))[0]
    check("comparison rows == 289533", row["n"] == 289533, f"got {row['n']}")

    row = query(
        "SELECT count(DISTINCT app_id) AS n FROM ca.comparison_rows"
        " WHERE run_id = %s AND diff_flag = 1", (run_id,))[0]
    check("apps with differences == 502", row["n"] == 502, f"got {row['n']}")

    row = query(
        "SELECT count(*) AS n FROM ca.app_segments WHERE run_id = %s AND decision_changed = 1",
        (run_id,))[0]
    check("decision flips == 385", row["n"] == 385, f"got {row['n']}")

    row = query("SELECT count(*) AS n FROM ca.app_segments WHERE run_id = %s", (run_id,))[0]
    check("segments rows == 2002", row["n"] == 2002, f"got {row['n']}")

    # --- 6. comparison against the organisers' reference (oracle) ---------
    import csv
    import pathlib

    ref_dir = pathlib.Path(
        "/home/mixantax/nuclearIThackWorkingDir/ХАКАТОН_СИСТЕМА_АНАЛИЗА_ИЗМЕНЕНИЙ"
        "/ПАКЕТ/ОРГАНИЗАТОРАМ/ЭТАЛОН"
    )
    mismatches = 0
    if ref_dir.exists():
        segs = {
            r["APP_ID"]: r
            for r in query(
                "SELECT app_id AS \"APP_ID\", decision_before AS \"DECISION_BEFORE\","
                " decision_after AS \"DECISION_AFTER\", decision_changed AS \"DECISION_CHANGED\","
                " score_okb_before AS \"SCORE_OKB_BEFORE\", score_okb_after AS \"SCORE_OKB_AFTER\","
                " pd_model_before AS \"PD_MODEL_BEFORE\", pd_model_after AS \"PD_MODEL_AFTER\","
                " cutoff_before AS \"CUTOFF_BEFORE\", cutoff_after AS \"CUTOFF_AFTER\""
                " FROM ca.app_segments WHERE run_id = %s",
                (run_id,),
            )
        }
        changed = {
            r["app_id"]
            for r in query(
                "SELECT DISTINCT app_id FROM ca.comparison_rows WHERE run_id=%s AND diff_flag=1",
                (run_id,),
            )
        }
        with open(ref_dir / "expected_OPEN.csv", encoding="utf-8-sig") as fh:
            for ref in csv.DictReader(fh, delimiter=";"):
                got = segs.get(ref["APP_ID"])
                if got is None:
                    mismatches += 1
                    continue
                if int(got["DECISION_CHANGED"]) != int(ref["DECISION_CHANGED"]):
                    mismatches += 1
                if (ref["APP_ID"] in changed) != (ref["CHANGED"] == "1"):
                    mismatches += 1
                for f in ("DECISION_BEFORE", "DECISION_AFTER", "PD_MODEL_BEFORE",
                          "PD_MODEL_AFTER", "CUTOFF_BEFORE", "CUTOFF_AFTER"):
                    if str(got[f] or "") != ref[f]:
                        mismatches += 1
        check("0 mismatches against expected_OPEN.csv", mismatches == 0, f"{mismatches} found")
    else:
        check("reference present", False, str(ref_dir))

    # --- 7. summary + pivot + drill-down ----------------------------------
    fs = FilterSpec()
    summ = report.summary(uid, run_id, fs)
    print("summary:", json.dumps(summ, ensure_ascii=False)[:700])

    t1 = time.perf_counter()
    piv = report.pivot(uid, run_id, ["KEY"], ["CHANGE_TYPE"], fs)
    t_pivot = (time.perf_counter() - t1) * 1000
    check("pivot returns cells", bool(piv.get("cells")), f"{len(piv.get('cells', {}))} cells, {t_pivot:.0f} ms")

    cell_key, cell = next(iter(piv["cells"].items()))
    t2 = time.perf_counter()
    drill = report.drill(uid, run_id, ["KEY"], ["CHANGE_TYPE"], fs, cell_key, page=1, page_size=50)
    t_drill = (time.perf_counter() - t2) * 1000
    check(
        "drill cell count == fact cnt",
        drill["total"] == cell["cnt"],
        f"cell={cell_key} cnt={cell['cnt']} drill_total={drill['total']} ({t_drill:.0f} ms)",
    )
    check("drill <= 300 ms", t_drill <= 300, f"{t_drill:.0f} ms")

    apps = report.apps_page(uid, run_id, fs, page=1, page_size=25)
    check("apps page size", len(apps["app_ids"]) == 25, f"total={apps['total']}")
    one = apps["app_ids"][0]
    detail = report.app_detail(uid, run_id, one)
    check("app detail has both slices", bool(detail), one)
    attrs = report.attribute_rows(uid, run_id, one, include_same=False)
    check("attribute rows for a changed app", len(attrs) > 0, f"{len(attrs)} rows")
    raw = report.raw_json(uid, run_id, one, "BEFORE")
    check("raw json available", bool(raw), f"{len(json.dumps(raw))} chars")

    # --- 8. check report ---------------------------------------------------
    rep = checks_service.check_report(uid, run_id)
    rows_ = rep.get("rows", [])
    check("check report has rows", len(rows_) >= 5, f"{len(rows_)} rows")
    print("check rows:", [(r["LABEL"][:28], r["ARGUMENT"][:26], r["RESULT"], r["CONFIDENCE"],
                           r["CNT"], r["TOTAL"]) for r in rows_[:4]])

    # --- 9. HTML export ----------------------------------------------------
    path = report.export_html(uid, run_id)
    import pathlib as _pl

    p = _pl.Path(path)
    text = p.read_text(encoding="utf-8")
    check("html export written", p.exists() and len(text) > 5000, f"{p} {len(text)} chars")
    check("html links back to the app", s.public_base_url in text, s.public_base_url)
    check("html has no external http resource",
          "cdn" not in text.lower() and "<script src" not in text.lower())

    # --- 10. user isolation ------------------------------------------------
    denied = 0
    for fn in (
        lambda: runs.get_run(uid2, run_id),
        lambda: report.summary(uid2, run_id, fs),
        lambda: checks_service.check_report(uid2, run_id),
        lambda: report.export_html(uid2, run_id),
    ):
        try:
            fn()
            denied += 1
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "AccessDenied":
                print(f"  unexpected exception type: {type(exc).__name__}: {exc}")
                denied += 1
    check("foreign user is denied everywhere", denied == 0, f"{denied} leak(s)")

    # --- verdict -----------------------------------------------------------
    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 70)
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    for name, ok, detail in bad:
        print(f"  FAILED: {name} {detail}")
    print(f"run_id={run_id} wall={wall:.1f}s")
    return 0 if not bad else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(3)
