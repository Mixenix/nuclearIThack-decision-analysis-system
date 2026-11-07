"""Run the browser drivers against a running server.

Two layers:

* ``ui_walkthrough.py`` — the demonstration path itself (вход → прогон → отчёт →
  сводная → заявка), 13 проверок. Это обязательная часть: она проходит тот же путь,
  который увидит жюри.
* ``ui_probe.py`` — вход, отклонение неверного пароля, отрисовка страниц и скриншоты.

Глубокие драйверы (``ui_create``, ``ui_workspace``, ``ui_acceptance``, ``ui_final``)
написаны против более ранней версии интерфейса и требуют обновления — они не входят
в обязательную проверку и запускаются вручную.

    tools/dev/playwright/run_isolated.py          # поднять изолированный стенд и прогнать
    UI_BASE=http://127.0.0.1:8000 ui_suite.py     # против работающего приложения
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
base = os.environ.get("UI_BASE", "http://127.0.0.1:8000")
env = {**os.environ, "UI_BASE": base}
failed: list[str] = []

for name in ("ui_probe", "ui_walkthrough"):
    print(f"Running {name}.py against {base}", flush=True)
    result = subprocess.run([sys.executable, "-u", str(root / f"{name}.py")], env=env)
    if result.returncode != 0:
        failed.append(name)

print()
if failed:
    print(f"провалились: {', '.join(failed)}")
    sys.exit(1)
print("браузерные проверки пройдены")
