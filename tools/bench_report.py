#!/usr/bin/env python3
"""Benchmark harness for ``docs/BENCHMARKS.md`` — every printed number is measured.

Design rules (PLAN.md §18.2, §21.5):

* every section prints its **measurement boundary**: what exactly is inside the
  timer and what is outside, so a reader can tell "stage" from "algorithm";
* no estimated, extrapolated or remembered numbers — if something could not be
  measured, the section says so and prints nothing else;
* compute phases use the *production* code (the same procedure files and the same
  ``stage_runner`` / ``check_runner`` subprocess entry points the worker uses), so
  the numbers describe the shipped path and not a private re-implementation;
* database writes are limited to ``TEMP`` tables plus, in ``full-run`` mode, one
  run whose name starts with ``bench-`` (removable with ``--delete-after``).

Usage (from the ``app/`` directory):

    .venv/bin/python tools/bench_report.py env
    .venv/bin/python tools/bench_report.py phases
    .venv/bin/python tools/bench_report.py compute
    .venv/bin/python tools/bench_report.py checks --run-id <run_id>
    .venv/bin/python tools/bench_report.py queries --run-id <run_id> --repeat 7
    .venv/bin/python tools/bench_report.py load --run-id <run_id>
    .venv/bin/python tools/bench_report.py full-run [--delete-after]
    .venv/bin/python tools/bench_report.py all --json /tmp/bench.json

``all`` runs env + phases + compute + checks + queries + load; ``full-run`` is
separate because it writes to the database and needs the worker to be running.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

APP_DIR = Path(__file__).resolve().parents[1]          # .../app
SRC_DIR = APP_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

BENCH_ROOT = APP_DIR / "artifacts" / "_bench"           # artifacts of this tool
DELIVERED_CHECKS = (
    "attribute_new_loss",
    "stop_factor_disappeared",
    "target_model_changed",
    "external_score_drift",
    "ar_drop_by_product",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def stats(samples_ms: Sequence[float]) -> dict[str, float]:
    """min / median / p95 / max of a sample, in milliseconds."""
    values = sorted(float(v) for v in samples_ms)
    if not values:
        return {"n": 0}
    index = min(len(values) - 1, max(0, round(0.95 * (len(values) - 1))))
    return {
        "n": len(values),
        "min_ms": round(values[0], 2),
        "median_ms": round(statistics.median(values), 2),
        "p95_ms": round(values[index], 2),
        "max_ms": round(values[-1], 2),
    }


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000.0


def repeat_ms(fn: Callable[[], Any], repeat: int) -> tuple[list[float], Any]:
    samples: list[float] = []
    value: Any = None
    for _ in range(max(1, repeat)):
        value, ms = timed(fn)
        samples.append(ms)
    return samples, value


def settings():
    from change_analysis.config import load_settings

    return load_settings()


_LOAD_LOG: list[dict[str, str]] = []


def say(title: str, note: str = "") -> None:
    load = _load_avg()
    _LOAD_LOG.append({"section": title, "at": now_iso(), "load_avg": load})
    print(f"\n### {title}")
    if note:
        print(f"# {note}")
    print(f"# load average 1/5/15 min: {load}")


def kv(key: str, value: Any, unit: str = "") -> None:
    text = f"{value}{(' ' + unit) if unit else ''}"
    print(f"{key:<52} {text}")


# ---------------------------------------------------------------------------
# env — hardware, versions, dataset shape
# ---------------------------------------------------------------------------


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _cpu_mhz() -> str:
    try:
        values = [
            float(line.split(":", 1)[1])
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
            if line.lower().startswith("cpu mhz")
        ]
        return f"{min(values):.0f}–{max(values):.0f}" if values else "unknown"
    except (OSError, ValueError):
        return "unknown"


def _mem_total_gb() -> str:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return f"{int(line.split()[1]) / 1024 / 1024:.1f}"
    except OSError:
        pass
    return "unknown"


def _mem_available_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def _load_avg() -> str:
    """1/5/15-minute load average — printed next to every section.

    The numbers in this tool are only comparable when the machine is quiet; other
    runs of the same application may be computing while a benchmark executes.
    """
    try:
        return " ".join(Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3])
    except OSError:
        return "unknown"


def _os_pretty() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _gpu() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc)}
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        return {"available": False, "reason": out.stderr.strip()[:200]}
    name, vram, driver = [part.strip() for part in text.split(",", 2)]
    try:
        cudf = md.version("cudf-cu13")
    except md.PackageNotFoundError:
        try:
            cudf = md.version("cudf")
        except md.PackageNotFoundError:
            cudf = ""
    return {"available": True, "name": name, "vram": vram, "driver": driver,
            "cudf": cudf or "not installed"}


def _package(name: str) -> str:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return "not installed"


def _pg_info() -> dict[str, Any]:
    from change_analysis.db.connection import query

    try:
        version = query("SELECT version() AS v")[0]["v"]
        settings_rows = query(
            "SELECT name, setting, unit FROM pg_settings WHERE name IN"
            " ('server_version','jit','work_mem','shared_buffers','effective_cache_size',"
            "'random_page_cost','max_parallel_workers_per_gather')"
        )
        databases = query(
            "SELECT count(*) AS n FROM ca.runs WHERE state = 'RUNNING'"
        )[0]["n"]
    except Exception as exc:  # noqa: BLE001 - the section must report, not crash
        return {"error": f"{type(exc).__name__}: {exc}"}
    formatted: dict[str, str] = {}
    for row in settings_rows:
        unit = row["unit"] or ""
        # memory settings report their value in 8 kB blocks, not in bytes
        if unit == "8kB":
            formatted[row["name"]] = f"{int(row['setting']) * 8 / 1024:.0f} MB"
        else:
            formatted[row["name"]] = f"{row['setting']}{(' ' + unit) if unit else ''}"
    return {
        "version": version.split(" on ")[0],
        "settings": formatted,
        "runs_running_now": int(databases),
    }


def dataset_info(root_ref: str | None = None) -> dict[str, Any]:
    """Files of the delivered dataset (no parsing, only stat + manifest)."""
    from change_analysis.services import datasets as datasets_service

    base = Path(settings().dataset_root).resolve()
    if root_ref:
        chosen = {"root_ref": root_ref, "pairs": 0, "ok": True}
    else:
        candidates = [d for d in datasets_service.discover_dataset_roots() if d["ok"]]
        if not candidates:
            raise SystemExit(f"no dataset with manifest.json under {base}")
        chosen = max(candidates, key=lambda d: d["pairs"])
    run_dir = (base / chosen["root_ref"]).resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    slices = manifest.get("srez") or {}
    before_dir = run_dir / slices["BEFORE"]["dir"]
    after_dir = run_dir / slices["AFTER"]["dir"]
    before = sorted(before_dir.glob("*.json"))
    after_names = {p.name for p in after_dir.glob("*.json")}
    apps = [p for p in before if p.name in after_names]
    total_bytes = sum(p.stat().st_size for p in apps) + sum(
        (after_dir / p.name).stat().st_size for p in apps
    )
    return {
        "root_ref": chosen["root_ref"],
        "run_dir": str(run_dir),
        "manifest": manifest,
        "apps": len(apps),
        "json_files": 2 * len(apps),
        "bytes": total_bytes,
        "bytes_per_response": total_bytes / max(1, 2 * len(apps)),
        "before_dir": str(before_dir),
        "after_dir": str(after_dir),
        "pairs": [{"app_id": p.stem, "before": str(p),
                   "after": str(after_dir / p.name)} for p in apps],
    }


def cmd_env(args: argparse.Namespace) -> dict[str, Any]:
    info = {
        "measured_at": now_iso(),
        "hostname": platform.node(),
        "os": _os_pretty(),
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "cpu_mhz": _cpu_mhz(),
        "mem_total_gb": _mem_total_gb(),
        "mem_available_gb": round(_mem_available_gb() or 0.0, 2),
        "gpu": _gpu(),
        "python": platform.python_version(),
        "packages": {name: _package(name) for name in (
            "pandas", "numpy", "psycopg", "psycopg-pool", "nicegui", "jinja2",
            "markupsafe", "argon2-cffi", "httpx",
        )},
        "postgres": _pg_info(),
    }
    data = dataset_info(args.dataset)
    info["dataset"] = {key: data[key] for key in
                       ("root_ref", "apps", "json_files", "bytes", "bytes_per_response")}
    info["dataset"]["bytes_human"] = human_bytes(data["bytes"])

    say("Machine", "measured by tools/bench_report.py env")
    kv("measured at", info["measured_at"])
    kv("OS", f"{info['os']} · kernel {info['kernel']}")
    kv("CPU", f"{info['cpu_model']}")
    kv("CPU logical cores / MHz", f"{info['cpu_count']} / {info['cpu_mhz']}")
    kv("RAM total / available now", f"{info['mem_total_gb']} GB / "
                                    f"{info['mem_available_gb']} GB")
    gpu = info["gpu"]
    kv("GPU", (f"{gpu['name']} · {gpu['vram']} · driver {gpu['driver']}"
               f" · cudf: {gpu['cudf']}") if gpu.get("available")
       else f"not usable: {gpu.get('reason')}")
    kv("Python", info["python"])
    for name, version in info["packages"].items():
        kv(f"  {name}", version)
    pg = info["postgres"]
    if "error" in pg:
        kv("PostgreSQL", f"unavailable: {pg['error']}")
    else:
        kv("PostgreSQL", pg["version"])
        for name, value in pg["settings"].items():
            kv(f"  {name}", value)
        kv("runs RUNNING in the database right now", pg["runs_running_now"])
    kv("dataset", f"{info['dataset']['root_ref']} · "
                  f"{info['dataset']['apps']} pairs · "
                  f"{info['dataset']['json_files']} JSON files · "
                  f"{info['dataset']['bytes_human']}")
    kv("  bytes per response", f"{info['dataset']['bytes_per_response']:.0f}")
    return info


# ---------------------------------------------------------------------------
# phases — one pass over the dataset, per-phase timers inside the loop
# ---------------------------------------------------------------------------


def _normalize_params() -> dict[str, Any]:
    from change_analysis.procedures import spr_normalize

    return dict(spr_normalize.DEFAULTS)


def cmd_phases(args: argparse.Namespace) -> dict[str, Any]:
    """read / parse / normalize / segments / jsonl-write, timed separately.

    Boundary: this is the normalize *algorithm* executed in-process; the
    subprocess start-up, the ``result.json`` write and the DB load are outside.
    One application is in memory at a time.
    """
    from change_analysis.pipeline.core import BucketRule, ArraySpec, make_adjuster, \
        normalize_document
    from change_analysis.pipeline.segments import build_product_segments, build_segment
    from change_analysis.worker.artifacts import JsonlWriter

    data = dataset_info(args.dataset)
    params = _normalize_params()
    specs = [ArraySpec(item["path"], item["mode"], tuple(item.get("key_fields") or ()))
             for item in params["arrays"]]
    excluded = tuple(params["excluded_paths"])
    rules = [BucketRule(item["path_prefix"], tuple(float(e) for e in item["edges"]))
             for item in params["buckets"]]
    adjust = make_adjuster(rules)

    out_dir = BENCH_ROOT / "phases"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    acc = {"read": 0.0, "parse": 0.0, "normalize": 0.0, "segments": 0.0, "write": 0.0}
    rows_total = 0
    segment_count = 0
    product_segment_count = 0
    rows_per_app: list[int] = []

    writer = JsonlWriter(out_dir / "rows.jsonl")
    wall_started = time.perf_counter()
    try:
        for pair in data["pairs"]:
            app_id = pair["app_id"]
            started = time.perf_counter()
            before_text = Path(pair["before"]).read_text(encoding="utf-8")
            after_text = Path(pair["after"]).read_text(encoding="utf-8")
            acc["read"] += time.perf_counter() - started

            started = time.perf_counter()
            before = json.loads(before_text)
            after = json.loads(after_text)
            acc["parse"] += time.perf_counter() - started
            del before_text, after_text

            started = time.perf_counter()
            produced: list[dict[str, Any]] = []
            for doc, source in ((before, "BEFORE"), (after, "AFTER")):
                for row in normalize_document(doc, source, app_id, specs, excluded, adjust):
                    produced.append({
                        "app_id": row.app_id, "source": row.source,
                        "prefix": row.prefix, "cls": row.cls, "suffix": row.suffix,
                        "array_key": row.array_key, "key": row.key, "path": row.path,
                        "value": row.value, "value_type": row.value_type,
                        "adjusted_value": row.adjusted_value, "pointer": row.pointer,
                    })
            acc["normalize"] += time.perf_counter() - started
            rows_per_app.append(len(produced))

            started = time.perf_counter()
            build_segment(app_id, before, after)
            built = build_product_segments(app_id, before, after)
            acc["segments"] += time.perf_counter() - started
            segment_count += 1
            product_segment_count += len(built)

            started = time.perf_counter()
            writer.write_many(produced)
            rows_total += len(produced)
            acc["write"] += time.perf_counter() - started
    finally:
        writer.close()
    wall = time.perf_counter() - wall_started

    result = {
        "measured_at": now_iso(),
        "apps": data["apps"],
        "rows": rows_total,
        "segments": segment_count,
        "product_segments": product_segment_count,
        "rows_per_app": stats(rows_per_app),
        "seconds": {key: round(value, 3) for key, value in acc.items()},
        "seconds_total": round(wall, 3),
        "rows_jsonl": str(out_dir / "rows.jsonl"),
        "rows_jsonl_bytes": (out_dir / "rows.jsonl").stat().st_size,
    }

    say("Normalize phases", "one application in memory at a time; each timer covers only "
                            "that phase; the JSONL write is the artifact write, "
                            "not a database write")
    kv("applications × slices", f"{result['apps']} × 2")
    kv("normalized rows", f"{result['rows']} ({result['rows'] / result['apps']:.1f} per pair)")
    for key in ("read", "parse", "normalize", "segments", "write"):
        share = 100.0 * result["seconds"][key] / result["seconds_total"]
        kv(f"  {key}", f"{result['seconds'][key]:.3f} s ({share:.0f} %)")
    kv("sum of phases", f"{result['seconds_total']:.3f} s")
    kv("minutes per response", result["rows_per_app"])
    kv("rows.jsonl", f"{human_bytes(result['rows_jsonl_bytes'])}")
    return result


# ---------------------------------------------------------------------------
# compute — the production stage entry points in their own interpreters
# ---------------------------------------------------------------------------


def _stage_spec(stage: str, run_id: str, out_dir: Path, code_path: Path,
                params: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return {"stage": stage, "run_id": run_id, "backend": "cpu",
            "out_dir": str(out_dir), "code_path": str(code_path),
            "params": params, "inputs": inputs}


def run_stage(stage: str, code_text: str, params: dict[str, Any],
              inputs: dict[str, Any], out_dir: Path,
              run_id: str = "00000000-0000-0000-0000-000000000000") -> dict[str, Any]:
    """Run one stage exactly the way the worker does: a fresh interpreter."""
    out_dir.mkdir(parents=True, exist_ok=True)
    code_path = out_dir / f"{stage}.py"
    code_path.write_text(code_text, encoding="utf-8")
    for stale in out_dir.glob("*.jsonl"):
        stale.unlink()
    spec = _stage_spec(stage, run_id, out_dir, code_path, params, inputs)
    spec_path = out_dir / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(SRC_DIR), "PYTHONUNBUFFERED": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": os.environ.get("LANG", "C.UTF-8"),
           "HOME": os.environ.get("HOME", "/tmp")}
    started = time.perf_counter()
    with (out_dir / "stdout.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            [sys.executable, "-m", "change_analysis.worker.stage_runner", str(spec_path)],
            stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(out_dir),
            check=False,
        )
    wall = time.perf_counter() - started
    result_file = out_dir / "result.json"
    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
    return {"stage": stage, "seconds": round(wall, 3),
            "reported_seconds": result.get("duration_sec"),
            "ok": bool(result.get("ok")) and completed.returncode == 0,
            "counts": result.get("counts") or {},
            "error": (result.get("error") or "")[:400],
            "out_dir": str(out_dir)}


def cmd_compute(args: argparse.Namespace) -> dict[str, Any]:
    """normalize → compare → aggregate through ``stage_runner`` (as the worker does).

    Boundary: the timer covers the subprocess start-up, the procedure, the JSONL
    artifact write and the ``result.json`` write.  The database load and the
    progress tailing done by the parent process are outside.
    """
    from change_analysis.services import procedures

    data = dataset_info(args.dataset)
    run_id = str(uuid.uuid4())
    root = BENCH_ROOT / "stages"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {"measured_at": now_iso(), "stages": {}, "run_id": run_id}
    outputs: dict[str, Path] = {}

    for stage in ("normalize", "compare", "aggregate"):
        selection = procedures.default_selection(stage)
        code = procedures.read_registered_file(selection["ref"])
        params = dict(_normalize_params()) if stage == "normalize" else {}
        if stage == "normalize":
            inputs = {"apps": data["pairs"], "dataset_root": str(settings().dataset_root)}
        elif stage == "compare":
            inputs = {"normalized": str(outputs["normalize"] / "rows.jsonl"),
                      "app_total": data["apps"]}
        else:
            comparison = outputs["compare"] / "rows.jsonl"
            inputs = {"comparison": str(comparison),
                      "expected_rows": sum(1 for _ in comparison.open("rb"))}
        out_dir = root / stage
        outcome = run_stage(stage, code, params, inputs, out_dir, run_id=run_id)
        outputs[stage] = out_dir
        results["stages"][stage] = outcome
        kv(f"stage {stage}",
           f"{outcome['seconds']:.3f} s · ok={outcome['ok']} · "
           f"counts={json.dumps(outcome['counts'], ensure_ascii=False)}")
        if not outcome["ok"]:
            kv(f"  {stage} error", outcome["error"])

    total = sum(item["seconds"] for item in results["stages"].values())
    results["compute_total_seconds"] = round(total, 3)
    kv("compute total (3 stages)", f"{total:.3f} s")
    for name in ("rows.jsonl", "segments.jsonl", "product_segments.jsonl",
                 "causes.jsonl", "facts.jsonl", "membership.jsonl"):
        for stage, out_dir in outputs.items():
            path = out_dir / name
            if path.exists():
                kv(f"  artifact {stage}/{name}", human_bytes(path.stat().st_size))
    results["artifacts"] = {stage: str(path) for stage, path in outputs.items()}
    (BENCH_ROOT / "latest_compute.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ---------------------------------------------------------------------------
# checks — the five delivered files through check_runner
# ---------------------------------------------------------------------------


def _checks_dsn() -> str:
    """Read-only role, same substitution the worker performs."""
    base = settings().database_url
    return base.replace("://ca:", "://ca_ro:", 1).replace("://ca@", "://ca_ro@", 1)


def cmd_checks(args: argparse.Namespace) -> dict[str, Any]:
    run_id = args.run_id or _reference_run_id()
    checks_dir = Path(settings().checks_dir)
    out_root = BENCH_ROOT / "checks"
    if out_root.exists():
        shutil.rmtree(out_root)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(SRC_DIR), "PYTHONUNBUFFERED": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": os.environ.get("LANG", "C.UTF-8"),
           "HOME": os.environ.get("HOME", "/tmp"), "DATABASE_URL": _checks_dsn()}

    say("Delivered checks", f"run {run_id}; one subprocess per check, as in production")
    rows: dict[str, Any] = {}
    total_ms = 0.0
    for check_id in DELIVERED_CHECKS:
        path = checks_dir / f"check_{check_id}.py"
        if not path.exists():
            kv(check_id, f"file not found: {path}")
            continue
        out_dir = out_root / check_id
        out_dir.mkdir(parents=True, exist_ok=True)
        spec_path = out_dir / "spec.json"
        spec_path.write_text(json.dumps({
            "run_id": run_id, "check_id": check_id,
            "code": path.read_text(encoding="utf-8"), "params": {},
            "source_ref": str(path), "source_kind": "file",
            "out_dir": str(out_dir),
        }, ensure_ascii=False), encoding="utf-8")
        started = time.perf_counter()
        with (out_dir / "stdout.log").open("w", encoding="utf-8") as log:
            subprocess.run(  # noqa: S603 - argv list, no shell
                [sys.executable, "-m", "change_analysis.worker.check_runner",
                 str(spec_path)], stdout=log, stderr=subprocess.STDOUT,
                env=env, cwd=str(out_dir), check=False,
            )
        wall = (time.perf_counter() - started) * 1000.0
        result_file = out_dir / "result.json"
        result = json.loads(result_file.read_text(encoding="utf-8")) \
            if result_file.exists() else {}
        duration = float(result.get("duration_ms") or 0)
        total_ms += wall
        rows[check_id] = {"wall_ms": round(wall, 1), "check_ms": duration,
                          "rows": result.get("rows", 0), "ok": bool(result.get("ok")),
                          "error": (result.get("error") or "")[:300]}
        kv(f"{check_id}", f"wall {wall:.0f} ms · inside check {duration} ms · "
                          f"rows {result.get('rows')} · ok={result.get('ok')}")
    kv("all five checks, wall clock", f"{total_ms:.0f} ms")
    return {"measured_at": now_iso(), "run_id": run_id, "checks": rows,
            "wall_ms_total": round(total_ms, 1)}


# ---------------------------------------------------------------------------
# queries — the analytical path the browser pages call
# ---------------------------------------------------------------------------


def _reference_run_id() -> str:
    """Newest SUCCEEDED run that processed the biggest dataset."""
    from change_analysis.db.connection import query

    rows = query(
        "SELECT id FROM ca.runs WHERE state = 'SUCCEEDED'"
        " ORDER BY expected_pairs DESC, created_at DESC LIMIT 1"
    )
    if not rows:
        raise SystemExit("no SUCCEEDED run in the database")
    return str(rows[0]["id"])


def _owner_of(run_id: str) -> str:
    from change_analysis.db.connection import query

    rows = query("SELECT owner_id FROM ca.runs WHERE id = %s", (run_id,))
    if not rows:
        raise SystemExit(f"run {run_id} not found")
    return str(rows[0]["owner_id"])


def cmd_queries(args: argparse.Namespace) -> dict[str, Any]:
    """Latency of the service calls each page performs, repeated N times.

    Boundary: server side only — the timer covers the SQL round trip and the
    Python assembly of the page data.  The NiceGUI websocket round trip and the
    browser rendering are outside (see docs/BENCHMARKS.md).
    """
    from change_analysis.contracts.types import FilterSpec
    from change_analysis.db.connection import query
    from change_analysis.services import checks as checks_service
    from change_analysis.services import report as report_service
    from change_analysis.services import runs as runs_service

    run_id = args.run_id or _reference_run_id()
    owner = _owner_of(run_id)
    repeat = max(1, args.repeat)
    all_filter = FilterSpec(run_id=run_id)
    filtered = FilterSpec(run_id=run_id, change_types=("VALUE_CHANGED",),
                          path_prefix="decisionResult.")
    running = query("SELECT count(*) AS n FROM ca.runs WHERE state = 'RUNNING'")[0]["n"]

    cases: list[tuple[str, Callable[[], Any]]] = [
        ("runs.get_run", lambda: runs_service.get_run(owner, run_id)),
        ("runs.list_runs", lambda: runs_service.list_runs(owner)),
        ("explore: apps_page(page_size=1000) [product filter list]",
         lambda: report_service.apps_page(owner, run_id, all_filter, page_size=1000)),
        ("explore: pivot default PATH × CHANGE_TYPE",
         lambda: report_service.pivot(owner, run_id, ["PATH"], ["CHANGE_TYPE"], all_filter)),
        ("explore: pivot KEY × CHANGE_TYPE",
         lambda: report_service.pivot(owner, run_id, ["KEY"], ["CHANGE_TYPE"], all_filter)),
        ("explore: pivot PREFIX+KEY × CHANGE_TYPE",
         lambda: report_service.pivot(owner, run_id, ["PREFIX", "KEY"],
                                      ["CHANGE_TYPE"], all_filter)),
        ("explore: pivot PATH × SOURCE",
         lambda: report_service.pivot(owner, run_id, ["PATH"], ["SOURCE"], all_filter)),
        ("explore: pivot filtered VALUE_CHANGED + prefix",
         lambda: report_service.pivot(owner, run_id, ["PATH"], ["CHANGE_TYPE"], filtered)),
        ("explore: drill largest diff cell (page 1)",
         lambda: report_service.drill(owner, run_id, ["PATH"], ["CHANGE_TYPE"],
                                      all_filter, "decisionResult.decision|VALUE_CHANGED")),
        ("explore: drill page 2", lambda: report_service.drill(
            owner, run_id, ["PATH"], ["CHANGE_TYPE"], all_filter,
            "decisionResult.decision|VALUE_CHANGED", page=2)),
        ("apps: apps_page(page_size=50)",
         lambda: report_service.apps_page(owner, run_id, all_filter, page_size=50)),
        ("app card: app_detail", None),   # filled below with a real app id
        ("app card: attribute_rows(diff only)", None),
        ("app card: raw_json(AFTER)", None),
        ("report page: checks_service.check_report",
         lambda: checks_service.check_report(owner, run_id)),
        ("report page: summary (8 cards)",
         lambda: report_service.summary(owner, run_id, all_filter)),
        ("report export: export_html (writes the file)",
         lambda: report_service.export_html(owner, run_id)),
    ]

    app_ids = report_service.drill(owner, run_id, ["PATH"], ["CHANGE_TYPE"], all_filter,
                                   "decisionResult.decision|VALUE_CHANGED",
                                   page=1, page_size=1)["app_ids"]
    sample_app = app_ids[0] if app_ids else "no-app"
    cases[11] = ("app card: app_detail",
                 lambda: report_service.app_detail(owner, run_id, sample_app))
    cases[12] = ("app card: attribute_rows(diff only)",
                 lambda: report_service.attribute_rows(owner, run_id, sample_app))
    cases[13] = ("app card: raw_json(AFTER)",
                 lambda: report_service.raw_json(owner, run_id, sample_app, "AFTER"))

    say("Analytical queries", f"run {run_id} · {repeat} repetitions each · "
                              f"server side only · runs RUNNING during measurement: {running}")
    results: dict[str, Any] = {"measured_at": now_iso(), "run_id": run_id,
                               "repeat": repeat, "runs_running": int(running),
                               "cases": {}}
    for name, fn in cases:
        if fn is None:
            continue
        samples, _ = repeat_ms(fn, repeat)
        data = stats(samples)
        results["cases"][name] = data
        kv(name, f"median {data['median_ms']:.1f} ms · p95 {data['p95_ms']:.1f} ms "
                 f"· min {data['min_ms']:.1f} · max {data['max_ms']:.1f}")

    composite_samples, _ = repeat_ms(
        lambda: (
            runs_service.get_run(owner, run_id),
            report_service.apps_page(owner, run_id, all_filter, page_size=1000),
            report_service.pivot(owner, run_id, ["PATH"], ["CHANGE_TYPE"], all_filter),
        ), repeat)
    composite = stats(composite_samples)
    results["cases"]["explore page open (get_run + product list + pivot)"] = composite
    kv("explore page open (composite)", f"median {composite['median_ms']:.1f} ms · "
                                        f"p95 {composite['p95_ms']:.1f} ms")

    # -- why the datamart exists: the same question over the raw comparison rows
    from change_analysis.services.report import _pivot_sql, _predicate

    where, params = _predicate(run_id, int(query(
        "SELECT revision FROM ca.runs WHERE id = %s", (run_id,))[0]["revision"]), all_filter)
    fact_sql = _pivot_sql(["PATH", "CHANGE_TYPE"], where)
    raw_sql = (
        "SELECT c.path, c.change_type, count(DISTINCT c.app_id) AS cnt"
        " FROM ca.comparison_rows c WHERE c.run_id = %s"
        " GROUP BY c.path, c.change_type"
    )
    fact_samples, _ = repeat_ms(lambda: query(fact_sql, params), repeat)
    raw_samples, _ = repeat_ms(lambda: query(raw_sql, [run_id]), repeat)
    fact_stats, raw_stats = stats(fact_samples), stats(raw_samples)
    results["pivot_source"] = {"facts_membership": fact_stats, "raw_comparison_rows": raw_stats}
    kv("pivot SQL over facts+membership", f"median {fact_stats['median_ms']:.1f} ms")
    kv("same grouping over raw comparison_rows", f"median {raw_stats['median_ms']:.1f} ms")

    plan = query("EXPLAIN (ANALYZE, BUFFERS) " + fact_sql, params)
    lines = [next(iter(row.values())) for row in plan]
    results["pivot_plan"] = lines
    kv("pivot plan (first 6 lines)", "")
    for line in lines[:6]:
        print(f"    {line}")

    return results


# ---------------------------------------------------------------------------
# load — COPY throughput and the COPY-vs-INSERT cost of the same rows
# ---------------------------------------------------------------------------

_NORMALIZED_COLUMNS = (
    "run_id", "app_id", "source", "path", "array_key", "prefix", "cls", "suffix",
    "key", "value", "value_type", "adjusted_value", "pointer",
)
_COMPARISON_COLUMNS = (
    "run_id", "app_id", "path", "array_key", "prefix", "cls", "suffix", "key",
    "value_before", "value_after", "adjusted_before", "adjusted_after", "diff_flag",
    "change_type", "present_before", "present_after", "value_type_before",
    "value_type_after",
)
_MEMBERSHIP_COLUMNS = ("run_id", "revision", "fact_id", "app_id")

# Scratch tables in ``ca`` (not TEMP): ``copy_rows`` takes its connection from the
# pool, so a TEMP table created on another connection is invisible to it.  They are
# dropped before and after every section, and they carry no indexes: index
# maintenance is *not* part of this number, it is part of the real load measured
# from the pipeline timestamps.
_DDL = {
    "normalized": """
        CREATE TABLE ca._bench_normalized (
            run_id uuid, app_id text, source text, path text, array_key text,
            prefix text, cls text, suffix text, key text, value text,
            value_type text, adjusted_value text, pointer text)""",
    "comparison": """
        CREATE TABLE ca._bench_comparison (
            run_id uuid, app_id text, path text, array_key text, prefix text,
            cls text, suffix text, key text, value_before text, value_after text,
            adjusted_before text, adjusted_after text, diff_flag smallint,
            change_type text, present_before boolean, present_after boolean,
            value_type_before text, value_type_after text)""",
    "membership": """
        CREATE TABLE ca._bench_membership (
            run_id uuid, revision integer, fact_id bigint, app_id text)""",
}


def _drop_scratch(names: Iterable[str]) -> None:
    from change_analysis.db.connection import execute

    for name in names:
        try:
            execute(f"DROP TABLE IF EXISTS ca._bench_{name}")
        except Exception:  # noqa: BLE001 - cleanup must never mask a result
            pass


def _jsonl_stream(path: Path, mapper: Callable[[dict], tuple]) -> Iterator[tuple]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield mapper(json.loads(line))


def _map_normalized(row: dict, run_id: str) -> tuple:
    return (run_id, row["app_id"], row["source"], row["path"], row["array_key"],
            row["prefix"], row["cls"], row["suffix"], row["key"], row["value"],
            row["value_type"], row["adjusted_value"], row.get("pointer") or "")


def _map_comparison(row: dict, run_id: str) -> tuple:
    return (run_id, row["app_id"], row["path"], row["array_key"], row["prefix"],
            row["cls"], row["suffix"], row["key"], row["value_before"],
            row["value_after"], row["adjusted_before"], row["adjusted_after"],
            row["diff_flag"], row["change_type"], row["present_before"],
            row["present_after"], row["value_type_before"], row["value_type_after"])


def _map_membership(row: dict, run_id: str) -> tuple:
    return (run_id, 1, int(row["fact_index"]) + 1, row["app_id"])


def cmd_load(args: argparse.Namespace) -> dict[str, Any]:
    """COPY throughput for the real artifacts, plus COPY vs row-by-row INSERT.

    Boundary: ``copy_rows`` is the production loader (pool + COPY + commit).  The
    scratch tables are real (so the WAL is written exactly as in production) but
    carry no indexes, and the ``causes`` UPDATE of the real load is not reproduced.
    Therefore these numbers are the *floor* of the real load; the authoritative
    end-to-end cost is the gap between stage timestamps in ``full-run``.
    """
    from change_analysis.db.connection import copy_rows, tx

    compute = json.loads((BENCH_ROOT / "latest_compute.json").read_text(encoding="utf-8")) \
        if (BENCH_ROOT / "latest_compute.json").exists() else {}
    artifacts = compute.get("artifacts") or {}
    if not artifacts:
        raise SystemExit("run `compute` first: no stage artifacts to load")
    run_id = str(uuid.uuid4())
    repeat = max(1, args.repeat)
    jobs = [
        ("normalized", Path(artifacts["normalize"]) / "rows.jsonl",
         _NORMALIZED_COLUMNS, _map_normalized),
        ("comparison", Path(artifacts["compare"]) / "rows.jsonl",
         _COMPARISON_COLUMNS, _map_comparison),
        ("membership", Path(artifacts["aggregate"]) / "membership.jsonl",
         _MEMBERSHIP_COLUMNS, _map_membership),
    ]
    _drop_scratch([name for name, _, _, _ in jobs])

    say("Database load", f"COPY through db.connection.copy_rows into real scratch "
                         f"tables without indexes · {repeat} repetitions each")
    results: dict[str, Any] = {"measured_at": now_iso(), "repeat": repeat, "tables": {}}
    for name, path, columns, mapper in jobs:
        if not path.exists():
            kv(name, f"artifact missing: {path}")
            continue
        rows = sum(1 for _ in path.open("rb"))
        size = path.stat().st_size
        samples: list[float] = []
        for _attempt in range(repeat):
            _drop_scratch([name])
            with tx() as conn, conn.cursor() as cur:
                cur.execute(_DDL[name])
            samples.append(timed(lambda: copy_rows(
                f"ca._bench_{name}", columns,
                _jsonl_stream(path, lambda r: mapper(r, run_id))
            ))[1])
        data = stats(samples)
        data.update({"rows": rows, "bytes": size,
                     "rows_per_sec": round(rows / (data["median_ms"] / 1000.0)),
                     "mb_per_sec": round(size / (data["median_ms"] / 1000.0) / 1e6, 1)})
        results["tables"][name] = data
        kv(f"COPY {name} ({rows} rows, {human_bytes(size)})",
           f"median {data['median_ms']:.0f} ms · {data['rows_per_sec']} rows/s · "
           f"{data['mb_per_sec']} MB/s · min {data['min_ms']:.0f} · max {data['max_ms']:.0f}")

    # -- COPY vs one INSERT per row on the same 20 000 comparison rows
    sample_path = Path(artifacts["compare"]) / "rows.jsonl"
    if sample_path.exists():
        limit = 20000
        payload: list[tuple] = []
        with sample_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(payload) >= limit:
                    break
                if line.strip():
                    payload.append(_map_comparison(json.loads(line), run_id))
        _drop_scratch(["insert"])
        with tx() as conn, conn.cursor() as cur:
            cur.execute(_DDL["comparison"].replace("_bench_comparison", "_bench_insert"))

        def insert_one_by_one() -> int:
            count = 0
            with tx() as conn, conn.cursor() as cur:
                for row in payload:
                    cur.execute(
                        "INSERT INTO ca._bench_insert VALUES ("
                        + ",".join(["%s"] * len(row)) + ")", row)
                    count += 1
            return count

        insert_ms = timed(insert_one_by_one)[1]
        _drop_scratch(["insert"])
        with tx() as conn, conn.cursor() as cur:
            cur.execute(_DDL["comparison"].replace("_bench_comparison", "_bench_insert"))
        copy_ms = timed(lambda: copy_rows(
            "ca._bench_insert", _COMPARISON_COLUMNS,
            (tuple(row) for row in payload)))[1]
        results["copy_vs_insert"] = {
            "rows": limit,
            "insert_one_by_one_ms": round(insert_ms, 1),
            "copy_ms": round(copy_ms, 1),
            "speedup": round(insert_ms / copy_ms, 1) if copy_ms else None,
        }
        kv(f"{limit} rows, one INSERT per row", f"{insert_ms / 1000:.2f} s")
        kv(f"{limit} rows, one COPY", f"{copy_ms / 1000:.3f} s")
        kv("COPY speed-up", f"x{insert_ms / copy_ms:.1f}")

    _drop_scratch([name for name, _, _, _ in jobs] + ["insert"])
    return results


# ---------------------------------------------------------------------------
# full-run — one real run through the queue, measured end to end
# ---------------------------------------------------------------------------


def cmd_full_run(args: argparse.Namespace) -> dict[str, Any]:
    """Create a ``bench-`` run, let the worker process it, measure the cycle.

    Boundary: ``wall_seconds`` is from ``create_run`` returning to the run row
    reaching a terminal state — queue wait included.  ``compute_seconds`` is
    ``jobs.started_at → runs.finished_at`` — queue wait excluded.  The database
    load phases are derived from the event timeline (stage finished → next stage
    started), because the worker does not log a separate marker for them.
    """
    from change_analysis.db.connection import query
    from change_analysis.services import configs, datasets, runs

    settings_obj = settings()
    owner = str(query("SELECT id FROM ca.users WHERE login = %s", (args.login,))[0]["id"])
    registered = [d for d in datasets.list_datasets(owner)
                  if d["app_pairs"] == (args.pairs or d["app_pairs"])]
    if not registered:
        registered = datasets.list_datasets(owner)
    if not registered:
        raise SystemExit("no dataset registered for this user — run the bootstrap first")
    dataset = max(registered, key=lambda d: d["app_pairs"])

    selections = configs.default_selections(owner)
    selections["checks"] = [{"id": check_id, "params": {}} for check_id in DELIVERED_CHECKS]
    name = args.name
    say("Full run", f"dataset {dataset['name']} · {dataset['app_pairs']} pairs · "
                    f"5 delivered checks · backend cpu")
    started = time.perf_counter()
    run_id = runs.create_run(owner, name, str(dataset["id"]), selections,
                             {"backend": "cpu"}, backend="cpu",
                             idempotency_key=uuid.uuid4().hex)
    kv("run created", run_id)

    deadline = time.time() + args.timeout
    claimed = False
    worker_started = False
    state = "QUEUED"
    while time.time() < deadline:
        job = query("SELECT state, started_at FROM ca.jobs WHERE run_id = %s"
                    " ORDER BY id DESC LIMIT 1", (run_id,))
        if job and job[0]["state"] in ("RUNNING", "SUCCEEDED"):
            claimed = True
        if job and job[0]["state"] == "QUEUED" and not worker_started \
                and time.time() - started > args.wait_claim:
            kv("no worker claimed the job in time", "starting a one-shot worker myself")
            subprocess.Popen(  # noqa: S603 - argv list, no shell
                [sys.executable, "-m", "change_analysis.worker.main", "--once"],
                env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
                cwd=str(APP_DIR), stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            worker_started = True
        state = query("SELECT state FROM ca.runs WHERE id = %s", (run_id,))[0]["state"]
        if state in ("SUCCEEDED", "FAILED", "INTERRUPTED"):
            break
        time.sleep(0.25)
    wall = time.perf_counter() - started

    run = query("SELECT * FROM ca.runs WHERE id = %s", (run_id,))[0]
    job = query("SELECT * FROM ca.jobs WHERE run_id = %s ORDER BY id DESC LIMIT 1",
                (run_id,))[0]
    steps = query("SELECT stage, state, started_at, finished_at FROM ca.run_steps"
                  " WHERE run_id = %s ORDER BY id", (run_id,))
    events = query("SELECT created_at, level, message FROM ca.run_events"
                   " WHERE run_id = %s ORDER BY id", (run_id,))
    executions = query("SELECT check_id, state, duration_ms, row_count"
                       " FROM ca.check_executions WHERE run_id = %s ORDER BY id", (run_id,))

    result: dict[str, Any] = {
        "measured_at": now_iso(),
        "run_id": run_id,
        "name": name,
        "state": state,
        "claimed_by_existing_worker": claimed,
        "wall_seconds": round(wall, 2),
        "compute_seconds": None,
        "summary": run["summary"],
        "error": run["error_message"],
        "stages": [],
        "loads": [],
        "checks": [],
        "events": [{"t": e["created_at"].strftime("%H:%M:%S.%f")[:-3],
                    "level": e["level"], "message": e["message"]} for e in events],
    }
    if job.get("started_at") and run.get("finished_at"):
        result["compute_seconds"] = round(
            (run["finished_at"] - job["started_at"]).total_seconds(), 2)

    for step in steps:
        if step["started_at"] and step["finished_at"]:
            seconds = (step["finished_at"] - step["started_at"]).total_seconds()
            result["stages"].append({"stage": step["stage"], "seconds": round(seconds, 2),
                                     "state": step["state"]})
            kv(f"stage {step['stage']}", f"{seconds:.2f} s ({step['state']})")

    # derived load windows: previous stage end -> next stage start
    def find_stage(name_: str) -> dict[str, Any] | None:
        return next((s for s in steps if s["stage"] == name_), None)

    normalize, compare, aggregate = (find_stage("normalize"), find_stage("compare"),
                                     find_stage("aggregate"))
    if normalize and compare and normalize["finished_at"] and compare["started_at"]:
        seconds = (compare["started_at"] - normalize["finished_at"]).total_seconds()
        result["loads"].append({"phase": "load normalize artifacts", "seconds": round(seconds, 2)})
        kv("load after normalize (normalized_rows + segments)", f"{seconds:.2f} s")
    if compare and aggregate and compare["finished_at"] and aggregate["started_at"]:
        seconds = (aggregate["started_at"] - compare["finished_at"]).total_seconds()
        result["loads"].append({"phase": "load comparison rows", "seconds": round(seconds, 2)})
        kv("load after compare (comparison_rows + causes)", f"{seconds:.2f} s")
    if aggregate and aggregate["finished_at"] and executions:
        first_check = min((e for e in events if "check " in (e["message"] or "")),
                          key=lambda e: e["created_at"], default=None)
        window_end = first_check["created_at"] if first_check else run.get("finished_at")
        if window_end:
            seconds = (window_end - aggregate["finished_at"]).total_seconds()
            first_duration = float(executions[0]["duration_ms"] or 0) / 1000.0
            derived = max(0.0, seconds - first_duration)
            result["loads"].append({"phase": "load aggregate artifacts",
                                    "seconds": round(derived, 2),
                                    "note": "first check's own duration subtracted"})
            kv("load after aggregate (facts + membership)",
               f"{derived:.2f} s (first check {first_duration * 1000:.0f} ms subtracted)")

    checks_ms = 0.0
    for execution in executions:
        checks_ms += float(execution["duration_ms"] or 0)
        result["checks"].append({"check_id": execution["check_id"],
                                 "state": execution["state"],
                                 "duration_ms": execution["duration_ms"],
                                 "rows": execution["row_count"]})
        kv(f"  check {execution['check_id']}",
           f"{execution['duration_ms']} ms · {execution['row_count']} rows · "
           f"{execution['state']}")
    result["checks_ms_total"] = round(checks_ms, 1)
    kv("all checks", f"{checks_ms:.0f} ms")

    if not result["stages"]:
        kv("WARNING", "no stage rows: the run did not reach the pipeline")
    if result["compute_seconds"]:
        kv("run cycle: created → finished", f"{wall:.2f} s (queue wait included)")
        kv("run cycle: job claimed → finished", f"{result['compute_seconds']:.2f} s")
    else:
        kv("run cycle: created → finished", f"{wall:.2f} s")
    if result["error"]:
        kv("error", result["error"][:400])

    if args.delete_after:
        clean_run(run_id, keep_artifacts=args.keep_artifacts)
        kv("cleanup", "bench run deleted; other runs untouched")
    else:
        kv("cleanup", f"kept — remove with: DELETE FROM ca.runs WHERE id = '{run_id}';")
    return result


def clean_run(run_id: str, keep_artifacts: bool = False) -> None:
    """Delete one *benchmark* run (guarded) and its artifact directory."""
    from change_analysis.db.connection import execute, query

    rows = query("SELECT id, name FROM ca.runs WHERE id = %s", (run_id,))
    if not rows:
        return
    if not str(rows[0]["name"]).startswith("bench-"):
        raise SystemExit(f"refusing to delete run {run_id}: name does not start with 'bench-'")
    execute("DELETE FROM ca.runs WHERE id = %s", (run_id,))
    if not keep_artifacts:
        target = settings().artifact_root / run_id
        if target.exists() and target.is_dir() and run_id in str(target):
            shutil.rmtree(target, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measured benchmarks for docs/BENCHMARKS.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", help="write the collected numbers to this file")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--dataset", default=None,
                                help="dataset root_ref under DATASET_ROOT "
                                     "(default: the biggest registered one)")

    env_parser = sub.add_parser("env", help="hardware, versions, dataset shape")
    add_common(env_parser)

    phases_parser = sub.add_parser("phases", help="read/parse/normalize/segments/write")
    add_common(phases_parser)

    compute_parser = sub.add_parser("compute", help="normalize/compare/aggregate stages")
    add_common(compute_parser)

    checks_parser = sub.add_parser("checks", help="the five delivered checks")
    checks_parser.add_argument("--run-id", default=None)

    queries_parser = sub.add_parser("queries", help="service latency behind the pages")
    queries_parser.add_argument("--run-id", default=None)
    queries_parser.add_argument("--repeat", type=int, default=7)

    load_parser = sub.add_parser("load", help="COPY throughput, COPY vs INSERT")
    load_parser.add_argument("--repeat", type=int, default=3)

    full_parser = sub.add_parser("full-run", help="one real bench- run end to end")
    full_parser.add_argument("--name", default="bench-full-cycle")
    full_parser.add_argument("--login", default="analyst")
    full_parser.add_argument("--pairs", type=int, default=0,
                             help="prefer the dataset with this many pairs")
    full_parser.add_argument("--timeout", type=float, default=900)
    full_parser.add_argument("--wait-claim", type=float, default=20)
    full_parser.add_argument("--delete-after", action="store_true")
    full_parser.add_argument("--keep-artifacts", action="store_true")

    all_parser = sub.add_parser("all", help="env + phases + compute + checks + queries + load")
    add_common(all_parser)
    all_parser.add_argument("--run-id", default=None)
    all_parser.add_argument("--repeat", type=int, default=7)

    args = parser.parse_args(argv)
    collected: dict[str, Any] = {"measured_at": now_iso(), "sections": {},
                                 "load_average_log": []}

    if args.command == "env":
        collected["sections"]["env"] = cmd_env(args)
    elif args.command == "phases":
        collected["sections"]["phases"] = cmd_phases(args)
    elif args.command == "compute":
        say("Compute stages", "each stage runs in its own interpreter, as in production")
        collected["sections"]["compute"] = cmd_compute(args)
    elif args.command == "checks":
        collected["sections"]["checks"] = cmd_checks(args)
    elif args.command == "queries":
        collected["sections"]["queries"] = cmd_queries(args)
    elif args.command == "load":
        collected["sections"]["load"] = cmd_load(args)
    elif args.command == "full-run":
        collected["sections"]["full_run"] = cmd_full_run(args)
    elif args.command == "all":
        say("Machine", "measured by tools/bench_report.py all")
        collected["sections"]["env"] = cmd_env(args)
        collected["sections"]["phases"] = cmd_phases(args)
        say("Compute stages", "each stage runs in its own interpreter, as in production")
        collected["sections"]["compute"] = cmd_compute(args)
        collected["sections"]["checks"] = cmd_checks(
            argparse.Namespace(run_id=args.run_id))
        collected["sections"]["queries"] = cmd_queries(
            argparse.Namespace(run_id=args.run_id, repeat=args.repeat))
        collected["sections"]["load"] = cmd_load(argparse.Namespace(repeat=3))

    collected["load_average_log"] = _LOAD_LOG
    if args.json:
        Path(args.json).write_text(json.dumps(collected, ensure_ascii=False,
                                              indent=2, default=str), encoding="utf-8")
        print(f"\nnumbers written to {args.json}")
    _shutdown()
    return 0


def _shutdown() -> None:
    """Close the psycopg pool so the process exits without a thread warning."""
    try:
        from change_analysis.db.connection import close_pool

        close_pool()
    except Exception:  # noqa: BLE001 - shutdown must never fail the run
        pass


if __name__ == "__main__":
    raise SystemExit(main())
