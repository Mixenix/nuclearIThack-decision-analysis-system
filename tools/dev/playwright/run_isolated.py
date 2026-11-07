#!/usr/bin/env python3
"""Runs the browser drivers against an isolated instance of the application.

The drivers in this directory drive a real browser against a real server. Doing
that against the demonstration database would leave configurations, procedures and
runs behind — so this runner creates a throw-away database, prepares it, starts the
application and a worker on port 8091, runs the drivers, and stops the processes.

    .venv/bin/python tools/dev/playwright/run_isolated.py
    .venv/bin/python tools/dev/playwright/run_isolated.py --keep   # не удалять базу

Requirements: PostgreSQL running (./scripts/pg.sh status), Chromium installed
(`.venv/bin/python -m playwright install chromium`), and the task package present
(DATASET_ROOT in .env) — the drivers build a real run from it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[3]
PY = APP / ".venv" / "bin" / "python"
PORT = 8091
DB = "change_analysis_ui"
BASE_DSN = os.environ.get("UI_DSN_BASE", "postgresql://ca:ca@127.0.0.1:55432")
DSN = f"{BASE_DSN}/{DB}"


def say(message: str) -> None:
    print(f"  {message}", flush=True)


def psql(database: str, sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", f"{BASE_DSN}/postgres", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )


def port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, seconds: float = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_busy(port):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="не удалять базу после прогона")
    args = parser.parse_args()

    if not PY.exists():
        print(f"нет интерпретатора {PY}", file=sys.stderr)
        return 2
    if port_busy(PORT):
        print(f"порт {PORT} уже занят — остановите тот процесс", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["DATABASE_URL"] = DSN
    env["PYTHONPATH"] = str(APP / "src")
    env.setdefault("APP_HOST", "127.0.0.1")
    env.setdefault("APP_PORT", str(PORT))
    env.setdefault("ARTIFACT_ROOT", "/tmp/ui-artifacts")

    print("\n1 · отдельная база")
    psql("postgres", f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    created = psql("postgres", f"CREATE DATABASE {DB} OWNER ca")
    if created.returncode != 0:
        print(created.stderr, file=sys.stderr)
        return 2
    say(f"создана {DB}")

    print("\n2 · схема, пользователи, датасет")
    prep = subprocess.run(
        [str(PY), "-c", (
            "from change_analysis.db.migrations import apply_migrations;"
            "from change_analysis.auth import service as auth;"
            "from change_analysis.services import datasets;"
            "from change_analysis.config import load_settings;"
            "import os;"
            "apply_migrations();"
            "uid = auth.ensure_user('qa', 'analyst', 'analyst');"
            "auth.ensure_user('qa2', 'analyst2', 'analyst2');"
            "s = load_settings();"
            # the drivers pick the dataset labelled "2002 пар" from the list
            "did = datasets.register_dataset(uid, 'Демо-прогон (2002 пар)', "
            "os.environ.get('UI_DATASET_REF', 'RUN-20260827-0001'), file_glob='*.json');"
            "print('dataset =', did, 'root =', s.dataset_root);"
        )],
        cwd=APP, env=env, capture_output=True, text=True,
    )
    print(prep.stdout.strip() or prep.stderr[-400:])
    if prep.returncode != 0:
        return 2

    print("\n3 · приложение и воркер на порту", PORT)
    web = subprocess.Popen([str(PY), "main.py"], cwd=APP, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = subprocess.Popen([str(PY), "-m", "change_analysis.worker.main"], cwd=APP,
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for_port(PORT):
            print("приложение не поднялось", file=sys.stderr)
            return 2
        say("готово")

        print("\n4 · браузерные сценарии")
        run = subprocess.run(
            [str(PY), "-u", str(Path(__file__).parent / "ui_suite.py")],
            cwd=APP, env={**env, "UI_BASE": f"http://127.0.0.1:{PORT}"},
        )
        return run.returncode
    finally:
        for name, process in (("воркер", worker), ("приложение", web)):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            say(f"{name} остановлен")
        if not args.keep:
            psql("postgres", f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            shutil.rmtree("/tmp/ui-artifacts", ignore_errors=True)
            say(f"база {DB} удалена")


if __name__ == "__main__":
    sys.exit(main())
