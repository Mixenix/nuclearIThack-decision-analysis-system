"""Check that the numbers promised in the documentation still match the system.

The project makes a point of never writing a number it has not measured.  That
promise is easy to break silently: someone edits a document, someone reruns the
pipeline, and the two drift apart.  This script turns the promise into a check.

    cd app && PYTHONPATH=src .venv/bin/python tools/check_doc_claims.py

How it works — deliberately literal, because a checker that guesses is worse
than no checker:

* ``canonical()`` reads the truth from the live system (the newest finished run,
  the holdout run, the check results, and pytest's collector);
* ``CLAIMS`` lists the exact sentences the documents are supposed to contain,
  with the number substituted.  A missing or different sentence is reported as
  ``file`` + the expected text;
* ``STALE`` lists strings that were true earlier and must not appear any more
  (an old test count, deleted run ids, the previous machine address), so a
  forgotten line is caught too.

It does not understand arbitrary prose: numbers outside these phrases are not
verified, and the script says so instead of pretending otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from change_analysis.config import load_settings
from change_analysis.db.connection import query

APP = Path(__file__).resolve().parents[1]


def canonical() -> dict[str, str]:
    """Read every promise from the live system, formatted the way docs write it."""
    load_settings()

    def one(sql: str) -> str:
        rows = query(sql)
        return str(rows[0][list(rows[0])[0]]) if rows else "?"

    def spaced(value: str) -> str:
        return f"{int(value):,}".replace(",", " ") if value.isdigit() else value

    latest = ("(SELECT id FROM ca.runs WHERE summary->>'pairs' IS NOT NULL"
              " ORDER BY created_at DESC LIMIT 1)")
    pairs = one("SELECT summary->>'pairs' FROM ca.runs WHERE summary->>'pairs' IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 1")
    rows = one("SELECT summary->>'rows' FROM ca.runs WHERE summary->>'rows' IS NOT NULL"
               " ORDER BY created_at DESC LIMIT 1")
    changed = one("SELECT summary->>'changed' FROM ca.runs WHERE summary->>'changed' IS NOT NULL"
                  " ORDER BY created_at DESC LIMIT 1")
    flips = one("SELECT summary->>'decision_changed' FROM ca.runs"
                " WHERE summary->>'decision_changed' IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 1")
    normalized = one(f"SELECT count(*) FROM ca.normalized_rows WHERE run_id = {latest}")
    # "rows in the report" is what the report actually shows: the published
    # revision, not every row ever written for this run.
    checks = one("SELECT count(*) FROM ca.check_results WHERE run_id = " + latest +
                 " AND revision = (SELECT max(revision) FROM ca.check_results"
                 f" WHERE run_id = {latest})")
    if checks == "0":  # no published revision yet: fall back to the run snapshot
        checks = one(f"SELECT count(*) FROM ca.check_results WHERE run_id = {latest}")
    holdout_pairs = one("SELECT summary->>'pairs' FROM ca.runs WHERE summary->>'pairs' = '998'"
                        " ORDER BY created_at DESC LIMIT 1")
    holdout_changed = one("SELECT summary->>'changed' FROM ca.runs"
                          " WHERE summary->>'pairs' = '998' ORDER BY created_at DESC LIMIT 1")
    holdout_flips = one("SELECT summary->>'decision_changed' FROM ca.runs"
                        " WHERE summary->>'pairs' = '998' ORDER BY created_at DESC LIMIT 1")

    collected = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
                               cwd=APP, capture_output=True, text=True)
    tests = sum(int(m.group(2)) for m in
                re.finditer(r"^(\S+\.py): (\d+)$", collected.stdout, re.MULTILINE))

    return {
        "pairs": spaced(pairs), "rows": spaced(rows), "changed": changed,
        "flips": flips, "normalized": spaced(normalized), "checks": checks,
        "holdout_pairs": holdout_pairs, "holdout_changed": holdout_changed,
        "holdout_flips": holdout_flips, "tests": str(tests),
    }


#: (file, slot, snippet with ``{slot}`` placeholders)
CLAIMS: list[tuple[str, str, str]] = [
    ("README.md", "rows", "| Строк в витрине сравнения | **{rows}** |"),
    ("README.md", "changed", "| Заявок с различиями по существу | **{changed}**"),
    ("README.md", "flips", "| Заявок со сменой решения | **{flips}** |"),
    ("README.md", "checks", "| Строк в отчёте проверок | **{checks}**"),
    ("README.md", "holdout_changed", "**{holdout_changed}** различий"),
    ("README.md", "holdout_flips", "**{holdout_flips}** смен решения"),
    ("README.md", "tests", "**{tests} тестов**"),
    ("README.md", "pairs", "**{pairs} пары**"),
    ("CONTRIBUTING.md", "pairs", "**{pairs} пары"),
    ("CONTRIBUTING.md", "changed", "**{changed} заявки с различиями"),
    ("CONTRIBUTING.md", "flips", "{flips} со сменой решения"),
    ("CONTRIBUTING.md", "tests", "{tests} тестов"),
    ("docs/STATUS.md", "tests", "**{tests} passed, 0 failed**"),
    ("docs/BENCHMARKS.md", "normalized", "{normalized} строк"),
    ("docs/ARCHITECTURE_NOTE.md", "normalized", "{normalized} строк"),
]

#: strings that were true once and must be gone now
STALE: list[tuple[str, str]] = [
    ("271 passed", "устаревшее число тестов (было до тестов форматов входа)"),
    ("818573ab", "удалённый прогон-эталон"),
    ("8475782e", "удалённый holdout-прогон"),
    ("1eda5da1", "удалённый дубль прогона"),
    ("40c4ad65", "удалённый дубль прогона"),
    ("c455dbfb", "удалённый дубль прогона"),
    ("192.168.31.231", "старый адрес машины"),
]


def main() -> int:
    values = canonical()
    print("истина по базе и по сбору тестов:")
    for key, value in values.items():
        print(f"  {key:16s} {value}")
    print()

    problems: list[str] = []
    checked = 0
    for rel, slot, template in CLAIMS:
        doc = APP / rel
        if not doc.exists():
            problems.append(f"{rel}: файла нет")
            continue
        wanted = template.format(**values)
        checked += 1
        if wanted not in doc.read_text(encoding="utf-8"):
            problems.append(f"{rel}: не найдено «{wanted}»  (проверяем {slot})")

    stale_hits: list[str] = []
    docs = [APP / "README.md", APP / "CONTRIBUTING.md",
            *sorted((APP / "docs").glob("*.md"))]
    for doc in docs:
        if not doc.exists():
            continue
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            for needle, why in STALE:
                if needle in line:
                    stale_hits.append(f"{doc.relative_to(APP)}:{lineno}: «{needle}» — {why}")

    if problems:
        print(f"НЕСООТВЕТСТВИЙ: {len(problems)} из {checked} утверждений\n")
        for line in problems:
            print("  " + line)
        print()
    else:
        print(f"все {checked} утверждений на месте и совпадают с системой")

    if stale_hits:
        print(f"\nУСТАРЕВШИЕ ФРАГМЕНТЫ: {len(stale_hits)}")
        for line in stale_hits:
            print("  " + line)

    if problems or stale_hits:
        print("\nчто не проверяется: числа вне перечисленных формулировок. "
              "Новое утверждение в документе — новая строка в CLAIMS.")
        return 1
    print("устаревших фрагментов нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
