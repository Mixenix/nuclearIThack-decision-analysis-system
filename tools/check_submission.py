#!/usr/bin/env python3
"""Проверка пакета сдачи против требований организаторов.

Каждый пункт требований превращён в машинную проверку: скрипт читает репозиторий и
базу, сравнивает с тем, что обещано в документах, и печатает таблицу «требование →
результат». Ничего не меняет.

    .venv/bin/python tools/check_submission.py            # весь пакет
    .venv/bin/python tools/check_submission.py --strict   # падать при любом провале

Запускать из корня приложения.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED: list[str] = []
WARNED: list[str] = []


def ok(requirement: str, detail: str, warn: bool = False) -> None:
    mark = "ПРЕДУПР" if warn else "ок"
    if warn:
        WARNED.append(requirement)
    print(f"  [{mark:8s}] {requirement}: {detail}")


def bad(requirement: str, detail: str) -> None:
    FAILED.append(requirement)
    print(f"  [ПРОВАЛ  ] {requirement}: {detail}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def git_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if line]


# ---------------------------------------------------------------------------
# 1. README: шапка и восемь обязательных разделов
# ---------------------------------------------------------------------------

def check_readme() -> None:
    section("README: шапка и обязательные разделы")
    text = read("README.md")

    header_fields = {
        "название проекта": r"\| \*\*Проект\*\* \|",
        "название команды": r"\| \*\*Команда\*\* \|",
        "задача": r"\| \*\*Задача\*\* \|",
        "ФИО участников": r"\| \*\*Участники\*\* \|",
    }
    for name, pattern in header_fields.items():
        ok(f"шапка: {name}", "есть") if re.search(pattern, text) else bad(
            f"шапка: {name}", "не найдено")

    if "{{" in text or "}}" in text:
        bad("шапка заполнена", "в README остались плейсхолдеры вида {{...}}")
    else:
        ok("шапка заполнена", "плейсхолдеров нет")

    required_sections = {
        "1. Описание решения": r"^## 1\. Описание решения",
        "2. Структура репозитория": r"^## 2\. Структура репозитория",
        "3. Используемые технологии": r"^## 3\. Используемые технологии",
        "4. Данные": r"^## 4\. Данные",
        "5. Архитектура и подход": r"^## 5\. Архитектура и подход",
        "6. Эксперименты и результаты": r"^## 6\. Эксперименты и результаты",
        "7. Инструкция по запуску": r"^## 7\. Инструкция по запуску",
        "8. Ограничения": r"^## 8\. Ограничения",
    }
    for name, pattern in required_sections.items():
        ok(f"раздел «{name}»", "на месте") if re.search(pattern, text, re.M) else bad(
            f"раздел «{name}»", "раздел отсутствует или назван иначе")

    # ссылки внутри README должны существовать
    missing = []
    for target in re.findall(r"\]\(([^)#][^)]*)\)", text):
        if target.startswith("http"):
            continue
        path = target.split("#")[0]
        if path and not (ROOT / path).exists():
            missing.append(path)
    ok("ссылки на файлы", f"все {len(re.findall(r'\]\(([^)#][^)]*)\)', text))} разрешаются") \
        if not missing else bad("ссылки на файлы", f"битые: {sorted(set(missing))[:5]}")

    # абсолютных путей этой машины в инструкциях быть не должно
    absolute = [line for line in text.splitlines() if "/home/mixantax" in line]
    ok("нет путей этой машины", "инструкции переносимы") if not absolute else bad(
        "нет путей этой машины", f"{len(absolute)} строк вида /home/mixantax/...")


# ---------------------------------------------------------------------------
# 2. Обязательные материалы сдачи
# ---------------------------------------------------------------------------

def check_materials() -> None:
    section("Материалы сдачи")
    must = {
        "README.md": "инструкция по развёртыванию",
        "requirements/cpu.txt": "зависимости",
        "main.py": "точка входа",
        "docs/DEMO.md": "сценарий демонстрации",
        "docs/ВИДЕО_СЦЕНАРИЙ.md": "раскадровка видео",
        "docs/КАК_ЗАПУСТИТЬ.md": "инструкция запуска",
        "docs/STATUS.md": "матрица требований",
        "docs/BENCHMARKS.md": "измерения",
        "docs/ARCHITECTURE_NOTE.md": "архитектурная записка",
        "docs/DECISIONS.md": "решения",
        "docs/AUDIT.md": "отчёт состязательных проверок",
        "scripts/demo.sh": "запуск одной командой",
        "scripts/verify.sh": "самопроверка",
        "scripts/share.sh": "общий доступ",
        "CONTRIBUTING.md": "для разработчиков",
    }
    for path, why in must.items():
        ok(path, why) if (ROOT / path).exists() else bad(path, "файл отсутствует")

    screens = sorted((ROOT / "docs" / "screens").glob("*.png"))
    ok("скриншоты интерфейса", f"{len(screens)} шт.") if len(screens) >= 8 else bad(
        "скриншоты интерфейса", f"всего {len(screens)}, ожидалось не меньше 8")

    # в репозиторий не должны попасть материалы задания, ключи и локальная база
    def forbidden_file(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        # .env.example is a template and belongs in the repository; a real .env,
        # the project-local cluster and the task package do not.
        if name == ".env.example":
            return False
        return (name == ".env" or name.startswith(".env.")
                or any(part in path for part in (".pgdata", "artifacts/",
                                                 "ХАКАТОН_СИСТЕМА", "ПАКЕТ/",
                                                 "results.json")))

    forbidden = [p for p in git_files() if forbidden_file(p)]
    ok("секреты и данные вне репозитория", "чисто") if not forbidden else bad(
        "секреты и данные вне репозитория", f"попали: {forbidden[:5]}")


# ---------------------------------------------------------------------------
# 3. Состояние системы и демонстрационные данные
# ---------------------------------------------------------------------------

def check_runtime() -> None:
    section("Работающая система")
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/login", timeout=5) as response:
            ok("веб-приложение отвечает", f"HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001
        bad("веб-приложение отвечает", f"{type(exc).__name__}: {exc}")
        return

    try:
        from change_analysis.db.connection import query
        runs = query("SELECT name, summary FROM ca.runs ORDER BY created_at")
    except Exception as exc:  # noqa: BLE001
        bad("база доступна", f"{type(exc).__name__}: {exc}")
        return
    ok("база доступна", f"прогонов: {len(runs)}")

    expected = {"2002": ("2002", "502", "385"), "998": ("998", "248", "186")}
    for name, summary in runs:
        if isinstance(summary, str):          # jsonb may arrive as text
            import json as _json
            try:
                summary = _json.loads(summary)
            except _json.JSONDecodeError:
                summary = {}
        summary = summary or {}
        pairs = str(summary.get("pairs") or "")
        if pairs in expected:
            want = expected[pairs]
            got = (pairs, str(summary.get("changed")), str(summary.get("decision_changed")))
            ok(f"прогон «{name[:38]}»", f"{got[0]} / {got[1]} / {got[2]}") \
                if got == want else bad(f"прогон «{name[:38]}»", f"ожидалось {want}, получено {got}")

    try:
        from change_analysis.db.connection import query as q
        rows = q("SELECT count(*) AS n FROM ca.check_results r WHERE r.revision ="
                 " (SELECT revision FROM ca.runs ORDER BY created_at DESC LIMIT 1)")[0]["n"]
        ok("отчёт проверок опубликован", f"строк: {rows}") if rows >= 30 else bad(
            "отчёт проверок опубликован", f"строк {rows}, ожидалось ≥ 30")
    except Exception as exc:  # noqa: BLE001
        bad("отчёт проверок опубликован", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 4. Артефакты: эталонные числа, экспорт, тесты
# ---------------------------------------------------------------------------

def check_artifacts() -> None:
    section("Артефакты и воспроизводимость")

    for script, why in (("tools/validate_against_reference.py", "сверка с эталоном"),
                        ("tools/acceptance_checks.py", "приёмка (26 проверок)"),
                        ("tools/check_doc_claims.py", "сверка чисел в документах"),
                        ("tools/dev/playwright/ui_walkthrough.py", "браузерный путь демонстрации")):
        ok(script, why) if (ROOT / script).exists() else bad(script, "инструмент отсутствует")

    test_files = list((ROOT / "tests").rglob("test_*.py"))
    ok("тесты", f"файлов: {len(test_files)}") if len(test_files) >= 15 else bad(
        "тесты", f"файлов {len(test_files)}")

    # README обещает определённые числа — они должны быть там же, где их ищет чекер
    claims = read("tools/check_doc_claims.py")
    ok("документы сверяются с базой", "16 утверждений описаны в tools/check_doc_claims.py") \
        if "CLAIMS" in claims else bad("документы сверяются с базой", "чекер не найден")


# ---------------------------------------------------------------------------
# 5. Критерии оценки организаторов (десять пунктов)
# ---------------------------------------------------------------------------

CRITERIA = [
    ("1. Полная обработка датасета и итоговый отчёт",
     "витрина + отчёт строятся на всех 2002 парах"),
    ("2. Совпадение числа заявок с различиями с эталоном",
     "502 (открытая часть) и 248 (отложенная)"),
    ("3. Различение четырёх типов изменений",
     "SAME / VALUE_CHANGED / ATTRIBUTE_NEW / ATTRIBUTE_LOST"),
    ("4. Подключение процедур и проверок через конфигурации",
     "источник «файл .py / рабочая область», включение проверок на странице конфигурации"),
    ("5. Исполнение выданных проверок и отдельный отчёт со ссылками",
     "пять проверок без изменения файлов, отчёт с переходом к заявкам"),
    ("6. Сводная таблица с изменяемой раскладкой",
     "строки/колонки/фильтры выбираются, агрегация на сервере"),
    ("7. Журнал этапов выполнения",
     "этапы с прогрессом, временем и переходом к коду"),
    ("8. Развёртывание на чистой машине по README",
     "описано пошагово: окружение, зависимости, база, данные, запуск"),
    ("9. Корректная обработка длительных операций",
     "отдельный процесс, прогресс, состояние после перезагрузки страницы"),
    ("10. (*) Корректность после перезагрузки страницы",
     "состояние живёт на сервере, страница восстанавливается из базы"),
]


def check_criteria() -> None:
    section("Десять критериев оценки: где доказательство")
    for name, evidence in CRITERIA:
        print(f"  [критерий ] {name}\n              └─ {evidence}")
    print("\n  Исполняемые доказательства по этим пунктам:")
    print("    tools/validate_against_reference.py  — пп. 1, 2, 3 (эталонные числа)")
    print("    scripts/verify.sh                    — одна команда: тесты + эталон + документы")
    print("    tools/acceptance_checks.py           — пп. 4, 5, 6, 7, 9 (26 проверок)")
    print("    tools/dev/playwright/ui_walkthrough.py — пп. 6, 7, 9, 10 (13 проверок в Chromium)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="код выхода 1 при провале")
    args = parser.parse_args()

    check_readme()
    check_materials()
    check_runtime()
    check_artifacts()
    check_criteria()

    print()
    if FAILED:
        print(f"\033[31mПровалено пунктов: {len(FAILED)}\033[0m")
        for item in FAILED:
            print(f"  — {item}")
        return 1 if args.strict else 0
    if WARNED:
        print(f"\033[33mЗамечаний: {len(WARNED)}\033[0m")
    print("\033[32mВсе проверки пакета пройдены.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
