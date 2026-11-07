"""Pipeline orchestration.

One job = one run of the stages ``normalize -> compare -> aggregate -> checks``.
Every algorithmically meaningful step is done in a **separate interpreter**
(PLAN.md §11.6): the worker only prepares inputs, tails progress, and bulk-loads
the produced artifacts into PostgreSQL.  The computing child gets no database
credentials, no session secret and no LLM key.

Progress is reported by the child through ``progress.jsonl``; the worker
throttles it into ``ca.run_steps`` / ``ca.run_events`` instead of writing one
database row per parsed JSON leaf (PLAN.md §14.3).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..config import Settings
from ..db.connection import copy_rows, execute, query, tx
from ..db.migrations import apply_migrations
from . import queue

SRC_DIR = Path(__file__).resolve().parents[2]

#: revision under which the comparison datamart is written; see migrations/0003
FACT_REVISION = 1


class StageFailed(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


@dataclass
class StageOutcome:
    stage: str
    out_dir: Path
    counts: dict[str, int]
    duration_sec: float
    meta: dict[str, Any]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def artifact_root(settings: Settings, run_id: str) -> Path:
    root = settings.artifact_root / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_progress(path: Path, offset: int) -> tuple[int, list[dict]]:
    """Read the newly appended progress lines without re-reading the file."""
    if not path.exists():
        return offset, []
    lines: list[dict] = []
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            text = raw.decode("utf-8", "replace").strip()
            if text:
                try:
                    lines.append(json.loads(text))
                except json.JSONDecodeError:
                    continue
    return offset, lines


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------


class PipelineJob:
    def __init__(self, job: dict[str, Any], settings: Settings) -> None:
        self.job = job
        self.settings = settings
        self.run_id = str(job["run_id"])
        self.job_id = int(job["id"])
        self.root = artifact_root(settings, self.run_id)
        self.seq = 0

    # -- run snapshot --------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        rows = query("SELECT snapshot, backend, name FROM ca.runs WHERE id = %s",
                     (self.run_id,))
        if not rows:
            raise StageFailed("prepare", f"run {self.run_id} disappeared")
        return rows[0]

    def _apps(self, snapshot: dict[str, Any], *, require_complete: bool = True) -> list[dict[str, str]]:
        dataset_root = Path(snapshot.get("abs_root") or self.settings.dataset_root).resolve()
        root_ref = snapshot.get("dataset_root_ref") or snapshot.get("root_ref") or ""
        run_dir = (dataset_root / root_ref).resolve() if root_ref else dataset_root
        before_dir = run_dir / snapshot["slices"]["BEFORE"]["dir"]
        after_dir = run_dir / snapshot["slices"]["AFTER"]["dir"]
        for path in (before_dir, after_dir):
            if not str(path.resolve()).startswith(str(dataset_root.resolve())):
                raise StageFailed("prepare", f"dataset path escapes DATASET_ROOT: {path}")
        if not before_dir.is_dir() or not after_dir.is_dir():
            raise StageFailed("prepare", f"slice directories not found under {run_dir}")

        names = sorted(p.name for p in before_dir.glob("*.json"))
        after_names = {p.name for p in after_dir.glob("*.json")}
        apps: list[dict[str, str]] = []
        broken: list[str] = []
        for name in names:
            app_id = name[:-5]
            if name not in after_names:
                broken.append(f"{app_id}: no AFTER slice: {after_dir / name}")
            apps.append({"app_id": app_id,
                         "before": str(before_dir / name),
                         "after": str(after_dir / name)})
        extra = after_names - {p.name for p in before_dir.glob("*.json")}
        for name in sorted(extra):
            broken.append(f"{name[:-5]}: no BEFORE slice: {before_dir / name}")
            apps.append({"app_id": name[:-5],
                         "before": str(before_dir / name),
                         "after": str(after_dir / name)})
        if broken and require_complete:
            # a batch run must not silently drop applications (PLAN.md §7)
            raise StageFailed(
                "prepare",
                f"{len(broken)} incomplete pairs, e.g. " + "; ".join(broken[:5]),
            )
        if not apps:
            raise StageFailed("prepare", "no application pairs found")
        return apps

    # -- one stage -----------------------------------------------------------
    def _run_stage(self, stage: str, code: str, params: dict[str, Any],
                   inputs: dict[str, Any], source_kind: str, source_ref: str,
                   progress_total: int) -> StageOutcome:
        self.seq += 1
        code_hash = _hash(code)
        code_path = self.root / "proc" / f"{stage}.py"
        code_path.parent.mkdir(parents=True, exist_ok=True)
        code_path.write_text(code, encoding="utf-8")

        out_dir = self.root / stage
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("*.jsonl"):
            stale.unlink()
        spec = {
            "stage": stage,
            "run_id": self.run_id,
            "backend": self.settings.compute_backend,
            "out_dir": str(out_dir),
            "code_path": str(code_path),
            "params": params,
            "inputs": inputs,
        }
        spec_path = out_dir / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        step_id = queue.start_step(
            self.run_id, self.job_id, stage, self.seq,
            procedure_kind=source_kind, procedure_ref=source_ref,
            procedure_hash=code_hash, total=progress_total,
        )

        argv = [self.settings.compute_python, "-m",
                "change_analysis.worker.stage_runner", str(spec_path)]
        if self.settings.compute_backend == "gpu":
            argv = [self.settings.compute_python, "-m", "cudf.pandas", "-m",
                    "change_analysis.worker.stage_runner", str(spec_path)]
        env = dict(self.settings.child_env())
        env["PYTHONPATH"] = str(SRC_DIR)

        started = time.perf_counter()
        log_path = out_dir / "stdout.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                argv, stdout=log, stderr=subprocess.STDOUT, env=env,
                cwd=str(out_dir),
            )
            offset = 0
            last_flush = 0.0
            timed_out = False
            while proc.poll() is None:
                time.sleep(0.4)
                offset, lines = read_progress(out_dir / "progress.jsonl", offset)
                for line in lines:
                    done = int(line.get("done", 0))
                    total = int(line.get("total", 0) or progress_total)
                    now = time.perf_counter()
                    if now - last_flush > 1.0:
                        queue.update_progress(step_id, self.run_id, done, total,
                                              pair_level=stage != "aggregate")
                        queue.heartbeat(self.job_id, self.settings.lease_sec)
                        last_flush = now
                    message = str(line.get("message") or "")
                    if message and now - last_flush > 1.0:
                        queue.add_event(self.run_id, step_id, "INFO", message)
                if time.perf_counter() - started > self.settings.stage_timeout_sec:
                    timed_out = True
                    proc.kill()
                    break
            code_returned = proc.wait()

        duration = time.perf_counter() - started
        offset, lines = read_progress(out_dir / "progress.jsonl", offset)
        if lines:
            last = lines[-1]
            queue.update_progress(step_id, self.run_id, int(last.get("done", 0)),
                                  int(last.get("total", 0) or progress_total),
                                  pair_level=stage != "aggregate")

        result_path = out_dir / "result.json"
        result: dict[str, Any] = {}
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
        for diagnostic in result.get("diagnostics") or []:
            queue.add_event(
                self.run_id, step_id, str(diagnostic.get("level") or "INFO").upper(),
                f"{diagnostic.get('code', '')} {diagnostic.get('where', '')}: "
                f"{diagnostic.get('message', '')}",
            )
        ok = bool(result.get("ok")) and code_returned == 0
        if timed_out:
            error = f"stage timed out after {self.settings.stage_timeout_sec}s"
        elif ok:
            error = ""
        else:
            error = str(result.get("error") or "").strip()
            if not error:
                tail = ""
                if log_path.exists():
                    tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-12:])
                error = f"exit code {code_returned}\n{tail}"
        if not ok:
            queue.end_step(step_id, "FAILED", error)
            queue.add_event(self.run_id, step_id, "ERROR",
                            f"stage {stage} failed: {error.splitlines()[0][:600]}")
            raise StageFailed(stage, error)
        queue.end_step(step_id, "DONE", None)
        queue.add_event(
            self.run_id, step_id, "INFO",
            f"stage {stage} finished in {duration:.1f}s" +
            (f" ({', '.join(f'{k}={v}' for k, v in result.get('counts', {}).items())})"
             if result.get("counts") else ""),
        )
        return StageOutcome(stage, out_dir, result.get("counts", {}) or {}, duration,
                            result.get("meta", {}) or {})

    # -- loading artifacts ---------------------------------------------------
    def _load_normalize(self, outcome: StageOutcome) -> None:
        with tx() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ca.normalized_rows WHERE run_id = %s", (self.run_id,))
            cur.execute("DELETE FROM ca.app_segments WHERE run_id = %s", (self.run_id,))
            cur.execute("DELETE FROM ca.product_segments WHERE run_id = %s", (self.run_id,))
            cur.execute("DELETE FROM ca.applications WHERE run_id = %s", (self.run_id,))
        rows_path = outcome.out_dir / "rows.jsonl"
        if rows_path.exists():
            def stream() -> Iterator[tuple]:
                with rows_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        r = json.loads(line)
                        yield (self.run_id, r["app_id"], r["source"], r["path"],
                               r["array_key"], r["prefix"], r["cls"], r["suffix"],
                               r["key"], r["value"], r["value_type"],
                               r["adjusted_value"], r.get("pointer") or "")
            copy_rows("ca.normalized_rows",
                      ("run_id", "app_id", "source", "path", "array_key", "prefix",
                       "cls", "suffix", "key", "value", "value_type",
                       "adjusted_value", "pointer"), stream())

        seg_path = outcome.out_dir / "segments.jsonl"
        if seg_path.exists():
            def segs() -> Iterator[tuple]:
                with seg_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        s = json.loads(line)
                        yield (self.run_id, s["APP_ID"], s["DECISION_BEFORE"],
                               s["DECISION_AFTER"], s["DECISION_CHANGED"], s["PRODUCTS"],
                               s["TOP_OFFER_BEFORE"], s["TOP_OFFER_AFTER"],
                               s["PARTNER_TYPE"], s["ROUTING_METHOD"],
                               s["PD_MODEL_BEFORE"], s["PD_MODEL_AFTER"],
                               s["CUTOFF_BEFORE"], s["CUTOFF_AFTER"],
                               s["REJECT_REASON_BEFORE"], s["REJECT_REASON_AFTER"],
                               s["SCORE_OKB_BEFORE"], s["SCORE_OKB_AFTER"],
                               s["REFERENCE_PRODUCT_ID"])
            copy_rows("ca.app_segments",
                      ("run_id", "app_id", "decision_before", "decision_after",
                       "decision_changed", "products", "top_offer_before",
                       "top_offer_after", "partner_type", "routing_method",
                       "pd_model_before", "pd_model_after", "cutoff_before",
                       "cutoff_after", "reject_reason_before", "reject_reason_after",
                       "score_okb_before", "score_okb_after", "reference_product_id"),
                      segs())
            # application index: flags for the application card and the owner check
            def application_rows() -> Iterator[tuple]:
                with seg_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        s = json.loads(line)
                        yield (self.run_id, s["APP_ID"],
                               json.dumps({
                                   "decision_changed": bool(s["DECISION_CHANGED"]),
                                   "products": s["PRODUCTS"].split(";") if s["PRODUCTS"] else [],
                                   "pd_model_before": s["PD_MODEL_BEFORE"],
                                   "pd_model_after": s["PD_MODEL_AFTER"],
                                   "cutoff_before": s["CUTOFF_BEFORE"],
                                   "cutoff_after": s["CUTOFF_AFTER"],
                                   "score_okb_before": s["SCORE_OKB_BEFORE"],
                                   "score_okb_after": s["SCORE_OKB_AFTER"],
                               }))
            copy_rows("ca.applications", ("run_id", "app_id", "flags"), application_rows())

        prod_path = outcome.out_dir / "product_segments.jsonl"
        if prod_path.exists():
            def products() -> Iterator[tuple]:
                with prod_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        p = json.loads(line)
                        yield (self.run_id, p["app_id"], p["source"], p["product_id"],
                               p["product_name"], p["partner_type"], p["routing_method"],
                               p["pd_model"], p["cutoff"], p["cutoff_value"],
                               p["decision"], p["reject_reason"], p["offer_rank"],
                               p["offer_type"])
            copy_rows("ca.product_segments",
                      ("run_id", "app_id", "source", "product_id", "product_name",
                       "partner_type", "routing_method", "pd_model", "cutoff",
                       "cutoff_value", "decision", "reject_reason", "offer_rank",
                       "offer_type"), products())

    def _load_compare(self, outcome: StageOutcome) -> None:
        execute("DELETE FROM ca.comparison_rows WHERE run_id = %s", (self.run_id,))
        path = outcome.out_dir / "rows.jsonl"
        if not path.exists():
            raise StageFailed("compare", "compare stage produced no rows.jsonl")

        def stream() -> Iterator[tuple]:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    yield (self.run_id, r["app_id"], r["path"], r["array_key"],
                           r["prefix"], r["cls"], r["suffix"], r["key"],
                           r["value_before"], r["value_after"],
                           r["adjusted_before"], r["adjusted_after"],
                           r["diff_flag"], r["change_type"], r["present_before"],
                           r["present_after"], r["value_type_before"],
                           r["value_type_after"])
        copy_rows("ca.comparison_rows",
                  ("run_id", "app_id", "path", "array_key", "prefix", "cls", "suffix",
                   "key", "value_before", "value_after", "adjusted_before",
                   "adjusted_after", "diff_flag", "change_type", "present_before",
                   "present_after", "value_type_before", "value_type_after"), stream())

        causes_path = outcome.out_dir / "causes.jsonl"
        if causes_path.exists():
            # observed signs of where the change came from, per application
            with tx() as conn, conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE tmp_causes (app_id text, category text,"
                    " evidence jsonb) ON COMMIT DROP"
                )
                with cur.copy("COPY tmp_causes (app_id, category, evidence) FROM STDIN") as copy:
                    with causes_path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            if not line.strip():
                                continue
                            item = json.loads(line)
                            copy.write_row((item["app_id"], item["category"],
                                            json.dumps(item.get("evidence") or [])))
                cur.execute(
                    "UPDATE ca.app_segments s SET cause_category = t.category"
                    " FROM tmp_causes t WHERE s.run_id = %s AND s.app_id = t.app_id",
                    (self.run_id,),
                )
                cur.execute(
                    "UPDATE ca.applications a SET cause_category = t.category,"
                    " flags = a.flags || jsonb_build_object('cause', t.category,"
                    " 'evidence', t.evidence)"
                    " FROM tmp_causes t WHERE a.run_id = %s AND a.app_id = t.app_id",
                    (self.run_id,),
                )

    def _load_aggregate(self, outcome: StageOutcome) -> None:
        # The comparison datamart has its own revision.  A later CHECKS_ONLY job
        # bumps runs.revision (check reports) and must never invalidate facts.
        revision = FACT_REVISION
        with tx() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ca.pivot_membership WHERE run_id = %s", (self.run_id,))
            cur.execute("DELETE FROM ca.pivot_facts WHERE run_id = %s", (self.run_id,))

        facts_path = outcome.out_dir / "facts.jsonl"
        if not facts_path.exists():
            raise StageFailed("aggregate", "aggregate stage produced no facts.jsonl")

        def facts() -> Iterator[tuple]:
            with facts_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    f = json.loads(line)
                    yield (self.run_id, revision, f["fact_index"], f["prefix"], f["cls"],
                           f["suffix"], f["array_key"], f["key"], f["path"],
                           f["value_adjusted"], f["diff_flag"], f["change_type"],
                           f["source"], f["cnt"])
        copy_rows("ca.pivot_facts",
                  ("run_id", "revision", "fact_index", "prefix", "cls", "suffix",
                   "array_key", "key", "path", "value_adjusted", "diff_flag",
                   "change_type", "source", "cnt"), facts())

        memb_path = outcome.out_dir / "membership.jsonl"
        if memb_path.exists():
            with tx() as conn, conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE tmp_membership (fact_index integer, app_id text)"
                    " ON COMMIT DROP"
                )
                with cur.copy("COPY tmp_membership (fact_index, app_id) FROM STDIN") as copy:
                    with memb_path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            if not line.strip():
                                continue
                            m = json.loads(line)
                            copy.write_row((int(m["fact_index"]), m["app_id"]))
                cur.execute(
                    "INSERT INTO ca.pivot_membership (run_id, revision, fact_id, app_id)"
                    " SELECT f.run_id, f.revision, f.fact_id, t.app_id"
                    " FROM tmp_membership t"
                    " JOIN ca.pivot_facts f"
                    "   ON f.run_id = %s AND f.revision = %s AND f.fact_index = t.fact_index",
                    (self.run_id, revision),
                )

    def _revision(self) -> int:
        rows = query("SELECT revision FROM ca.runs WHERE id = %s", (self.run_id,))
        return int(rows[0]["revision"]) if rows else 1

    # -- checks --------------------------------------------------------------
    def run_checks(self, checks: list[dict[str, Any]], revision: int,
                   publish: bool = True) -> None:
        """Execute every check, then publish the whole report in one transaction.

        Two properties matter and are easy to get wrong:

        * the previous revision stays readable until the new one is complete, so a
          failed run never leaves the analyst with an empty report;
        * rows of *other* revisions are never deleted — this used to wipe them.
        """
        self.seq += 1
        total = len(checks)
        outcomes: list[dict[str, Any]] = []

        for index, spec in enumerate(checks, start=1):
            check_id = spec["id"]
            out_dir = self.root / "checks" / check_id
            out_dir.mkdir(parents=True, exist_ok=True)
            spec_path = out_dir / "spec.json"
            spec_path.write_text(json.dumps({
                "run_id": self.run_id,
                "check_id": check_id,
                "code": spec["code"],
                "params": spec.get("params") or {},
                "source_ref": spec.get("source_ref", ""),
                "source_kind": spec.get("source_kind", "workspace"),
                "out_dir": str(out_dir),
            }, ensure_ascii=False), encoding="utf-8")

            argv = [self.settings.compute_python, "-m",
                    "change_analysis.worker.check_runner", str(spec_path)]
            env = dict(self.settings.child_env())
            env["PYTHONPATH"] = str(SRC_DIR)
            env["DATABASE_URL"] = self._checks_dsn()
            queue.add_event(self.run_id, None, "INFO",
                            f"check {index}/{total}: {check_id} started")
            started = time.perf_counter()
            try:
                with (out_dir / "stdout.log").open("w", encoding="utf-8") as log:
                    subprocess.run(  # noqa: S603 - argv list, no shell
                        argv, stdout=log, stderr=subprocess.STDOUT, env=env,
                        cwd=str(out_dir),
                        timeout=max(60, self.settings.stage_timeout_sec // 4),
                        check=False,
                    )
                duration_ms = int((time.perf_counter() - started) * 1000)
                result_path = out_dir / "result.json"
                result = json.loads(result_path.read_text(encoding="utf-8")) \
                    if result_path.exists() else {
                        "ok": False, "error": "check produced no result"}
            except subprocess.TimeoutExpired:
                duration_ms = int((time.perf_counter() - started) * 1000)
                result = {"ok": False, "error": "check timed out"}
            rows = []
            if result.get("ok"):
                rows = json.loads((out_dir / "rows.json").read_text(encoding="utf-8"))
            outcomes.append({
                "spec": spec, "rows": rows, "duration_ms": duration_ms,
                "error": "" if result.get("ok") else str(result.get("error") or "unknown"),
            })
            queue.add_event(
                self.run_id, None,
                "INFO" if result.get("ok") else "ERROR",
                f"check {check_id}: " + (f"{len(rows)} rows in {duration_ms} ms"
                                         if result.get("ok")
                                         else f"failed: {outcomes[-1]['error'][:300]}"),
            )

        if not publish:
            return

        with tx() as conn, conn.cursor() as cur:
            # replace exactly this revision; older published revisions stay valid
            cur.execute("DELETE FROM ca.check_results WHERE run_id = %s AND revision = %s",
                        (self.run_id, revision))
            cur.execute("DELETE FROM ca.check_executions WHERE run_id = %s AND revision = %s",
                        (self.run_id, revision))
            for outcome in outcomes:
                spec = outcome["spec"]
                state = "DONE" if outcome["rows"] or not outcome["error"] else "ERROR"
                cur.execute(
                    "INSERT INTO ca.check_executions (run_id, revision, check_id, label,"
                    " source_kind, source_ref, params, state, duration_ms, row_count,"
                    " error, spec)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)"
                    " RETURNING id",
                    (self.run_id, revision, spec["id"], spec.get("label", ""),
                     spec.get("source_kind", "workspace"), spec.get("source_ref", ""),
                     json.dumps(spec.get("params") or {}), state,
                     outcome["duration_ms"], len(outcome["rows"]),
                     outcome["error"][:4000] or None,
                     json.dumps({**spec, "params": spec.get("params") or {}})),
                )
                execution_id = int(cur.fetchone()["id"])
                for row in outcome["rows"]:
                    cur.execute(
                        "INSERT INTO ca.check_results (execution_id, run_id, revision,"
                        " label, source, argument, cnt, total, metric, app_ids,"
                        " result, confidence)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (execution_id, self.run_id, revision,
                         str(row["LABEL"])[:500], str(row["SOURCE"]),
                         str(row["ARGUMENT"])[:1000], int(row["CNT"]),
                         int(row["TOTAL"]),
                         None if row.get("METRIC") is None else float(row["METRIC"]),
                         list(row["APP_IDS"]), str(row["RESULT"]),
                         str(row["CONFIDENCE"])),
                    )
            cur.execute("UPDATE ca.runs SET revision = %s WHERE id = %s",
                        (revision, self.run_id))

    def _checks_dsn(self) -> str:
        """Read-only DSN for user check code (falls back to the normal one)."""
        base = self.settings.database_url
        if "ca_ro" in base:
            return base
        return base.replace("://ca:", "://ca_ro:", 1).replace("://ca@", "://ca_ro@", 1)

    # -- summary -------------------------------------------------------------
    def _summarise(self) -> dict[str, Any]:
        rows = query(
            """
            SELECT
              (SELECT count(*) FROM ca.app_segments WHERE run_id = %(run)s) AS pairs,
              (SELECT count(DISTINCT app_id) FROM ca.comparison_rows
                WHERE run_id = %(run)s AND diff_flag = 1) AS changed,
              (SELECT count(*) FROM ca.app_segments
                WHERE run_id = %(run)s AND decision_changed = 1) AS decision_changed,
              (SELECT count(*) FROM ca.comparison_rows WHERE run_id = %(run)s) AS rows,
              (SELECT count(*) FROM ca.pivot_facts WHERE run_id = %(run)s) AS facts
            """,
            {"run": self.run_id},
        )
        return {k: int(v) for k, v in rows[0].items()}

    # -- entry point ---------------------------------------------------------
    def run(self) -> None:
        snapshot_row = self._snapshot()
        snapshot = snapshot_row["snapshot"]
        queue.add_event(self.run_id, None, "INFO", "job started")
        # Let normalize report incomplete pairs in its artifacts and events.
        apps = self._apps(snapshot, require_complete=False)
        execute("UPDATE ca.runs SET expected_pairs = %s WHERE id = %s",
                (len(apps), self.run_id))

        procedures = snapshot.get("procedures") or {}
        try:
            norm = self._run_stage(
                "normalize", procedures["normalize"]["code"],
                procedures["normalize"].get("params") or {},
                {"apps": apps, "dataset_root": str(self.settings.dataset_root)},
                procedures["normalize"].get("source_kind", "workspace"),
                procedures["normalize"].get("source_ref", ""),
                progress_total=len(apps),
            )
            self._load_normalize(norm)

            cmp_ = self._run_stage(
                "compare", procedures["compare"]["code"],
                procedures["compare"].get("params") or {},
                {"normalized": str(norm.out_dir / "rows.jsonl"),
                 "app_total": len(apps)},
                procedures["compare"].get("source_kind", "workspace"),
                procedures["compare"].get("source_ref", ""),
                progress_total=len(apps),
            )
            self._load_compare(cmp_)

            cmp_rows = cmp_.out_dir / "rows.jsonl"
            expected = 0
            if cmp_rows.exists():
                with cmp_rows.open("rb") as fh:
                    for _ in fh:
                        expected += 1
            agg = self._run_stage(
                "aggregate", procedures["aggregate"]["code"],
                procedures["aggregate"].get("params") or {},
                {"comparison": str(cmp_rows), "expected_rows": expected},
                procedures["aggregate"].get("source_kind", "workspace"),
                procedures["aggregate"].get("source_ref", ""),
                progress_total=expected,
            )
            self._load_aggregate(agg)

            self.run_checks(snapshot.get("checks") or [], self._revision())

            summary = self._summarise()
            with tx() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE ca.runs SET summary = %s::jsonb,"
                    " processed_pairs = expected_pairs WHERE id = %s",
                    (json.dumps(summary), self.run_id),
                )
            # Warm the report paths before announcing the run as finished: a
            # SUCCEEDED run must open fast, not 77 s later once the pages happen
            # to be in shared buffers (PLAN §13.2 — publish only what is ready).
            self._warm_reads()
            queue.finish(self.job_id, self.run_id, "SUCCEEDED", run_state="SUCCEEDED")
            queue.add_event(self.run_id, None, "INFO",
                            f"run finished: {summary.get('changed')} applications with "
                            f"differences, {summary.get('decision_changed')} decision flips")
        except StageFailed as exc:
            self._fail(exc.stage, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._fail("pipeline", f"{type(exc).__name__}: {exc}")
        finally:
            self._purge_large_artifacts()

    def _warm_reads(self) -> None:
        """Warm what the analyst opens first, and refresh planner statistics.

        Right after a bulk load the first summary/pivot request can take tens of
        seconds: the pages are not in shared buffers yet and the load itself
        triggers an autovacuum on the very tables the report reads. The same
        request a moment later takes under a second. Measured here: 77 s for the
        first summary against 0.4-0.5 s for the next ones. Paying ~2 s of worker
        time removes that cliff, and the number is logged rather than hidden.
        """
        started = __import__("time").perf_counter()
        try:
            execute("ANALYZE ca.comparison_rows, ca.app_segments, ca.pivot_facts,"
                    " ca.pivot_membership")
            for sql in (
                "SELECT count(*) FROM ca.comparison_rows WHERE run_id = %s",
                "SELECT count(*) FROM ca.pivot_membership WHERE run_id = %s",
                "SELECT count(DISTINCT app_id) FROM ca.comparison_rows"
                " WHERE run_id = %s AND diff_flag = 1",
            ):
                query(sql, (self.run_id,))
            elapsed = __import__("time").perf_counter() - started
            queue.add_event(self.run_id, None, "INFO",
                            f"read warm-up and ANALYZE finished in {elapsed:.1f}s")
        except Exception as exc:  # noqa: BLE001 - warming must never fail a run
            queue.add_event(self.run_id, None, "WARN",
                            f"read warm-up skipped: {type(exc).__name__}: {exc}")

    def _fail(self, stage: str, message: str) -> None:
        queue.finish(self.job_id, self.run_id, "FAILED", error=message[:4000],
                     error_code=f"STAGE_FAILED:{stage}", run_state="FAILED")
        queue.add_event(self.run_id, None, "ERROR", f"{stage} failed: {message[:1500]}")

    def _purge_large_artifacts(self) -> None:
        """Keep the logs and the small artifacts, drop the multi-hundred-MB ones."""
        for pattern in ("normalize/rows.jsonl", "compare/rows.jsonl",
                        "aggregate/membership.jsonl"):
            path = self.root / pattern
            if path.exists():
                path.unlink()


# ---------------------------------------------------------------------------
# checks-only and LLM jobs
# ---------------------------------------------------------------------------


def run_checks_only(job: dict[str, Any], settings: Settings) -> None:
    pipeline = PipelineJob(job, settings)
    snapshot = pipeline._snapshot()["snapshot"]
    revision = int(query("SELECT revision FROM ca.runs WHERE id = %s",
                         (pipeline.run_id,))[0]["revision"])
    try:
        # Republish the complete configured set: a report revision must be
        # internally consistent, so the new check joins the existing ones instead
        # of replacing them.
        pipeline.run_checks(snapshot.get("checks") or [], revision)
        queue.finish(pipeline.job_id, pipeline.run_id, "SUCCEEDED")
    except Exception as exc:  # noqa: BLE001
        queue.finish(pipeline.job_id, pipeline.run_id, "FAILED",
                     error=f"{type(exc).__name__}: {exc}"[:2000],
                     error_code="CHECKS_FAILED")


def run_llm_job(job: dict[str, Any], settings: Settings) -> None:
    from ..llm.adapter import request_second_opinion

    run_id = str(job["run_id"])
    revision = int(query("SELECT revision FROM ca.runs WHERE id = %s",
                         (run_id,))[0]["revision"])
    try:
        result = request_second_opinion(run_id, revision, job.get("payload") or {})
        state = result.get("state", "ERROR")
        with tx() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ca.llm_reports WHERE run_id = %s AND revision = %s",
                        (run_id, revision))
            cur.execute(
                "INSERT INTO ca.llm_reports (run_id, revision, provider, model,"
                " payload_hash, state, result, error, finished_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s, now())",
                (run_id, revision, settings.llm_provider, settings.llm_model,
                 result.get("payload_hash", ""), state,
                 json.dumps(result, ensure_ascii=False), result.get("error", "")),
            )
        queue.finish(int(job["id"]), run_id, "SUCCEEDED")
    except Exception as exc:  # noqa: BLE001
        queue.finish(int(job["id"]), run_id, "FAILED",
                     error=f"{type(exc).__name__}: {exc}"[:2000], error_code="LLM_FAILED")
