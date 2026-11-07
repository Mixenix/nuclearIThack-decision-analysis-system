#!/usr/bin/env python3
"""Browser walkthrough: the demonstration script, executed by a real browser.

Проходит ровно тот путь, который увидит жюри, и складывает скриншоты:

    вход → список прогонов → страница прогона → отчёт проверок → сводная →
    переход к заявке → карточка заявки

    .venv/bin/python tools/dev/playwright/ui_walkthrough.py            # последний прогон
    .venv/bin/python tools/dev/playwright/ui_walkthrough.py <run_id>
    UI_BASE=http://127.0.0.1:8091 ... ui_walkthrough.py                # другой адрес

Возвращает 1, если хоть одна проверка упала.
"""

from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

APP = pathlib.Path(__file__).resolve().parents[3]
BASE = os.environ.get("UI_BASE", "http://127.0.0.1:8000")
SHOTS = pathlib.Path(os.environ.get("UI_SHOTS", APP / "docs" / "screens"))
SHOTS.mkdir(parents=True, exist_ok=True)
USER = os.environ.get("UI_USER", "analyst")
PASSWORD = os.environ.get("UI_PASSWORD", "analyst")


def _latest_run() -> str:
    """Pick the newest finished run of that user, so the script needs no arguments."""
    sys.path.insert(0, str(APP / "src"))
    from change_analysis.auth import service as auth
    from change_analysis.db.connection import query

    uid = auth.authenticate(USER, PASSWORD)
    if not uid:
        print("не удалось войти: проверьте UI_USER/UI_PASSWORD", file=sys.stderr)
        return ""
    rows = query("SELECT id FROM ca.runs WHERE owner_id = %s AND state = 'SUCCEEDED'"
                 " ORDER BY created_at DESC LIMIT 1", (uid,))
    return str(rows[0]["id"]) if rows else ""


run_id = sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("UI_RUN_ID") or _latest_run())
if not run_id:
    print("нет готового прогона: сначала запустите прогон через интерфейс или demo.sh",
          file=sys.stderr)
    sys.exit(3)
ok: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    ok.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def text_of(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="ru-RU")
    page = ctx.new_page()
    page.set_default_timeout(20000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # ---- 1. login -------------------------------------------------------
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.screenshot(path=str(SHOTS / "01-login.png"), full_page=True)
    body = text_of(page)
    check("login page renders", "Логин" in body or "Вход" in body, body[:80].replace("\n", " "))

    inputs = page.locator("input")
    n = inputs.count()
    check("login has two fields", n >= 2, f"{n} inputs")
    inputs.nth(0).fill(USER)
    inputs.nth(1).fill(PASSWORD)
    clicked = False
    for label in ("Войти", "Вход", "Login"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            clicked = True
            break
    if not clicked:
        inputs.nth(1).press("Enter")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(SHOTS / "02-runs.png"), full_page=True)
    body = text_of(page)
    check("logged in (run list visible)", "Прогон" in body or "Запуск" in body,
          body[:120].replace("\n", " "))

    # ---- 2. run page ----------------------------------------------------
    page.goto(f"{BASE}/runs/{run_id}", wait_until="networkidle")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(SHOTS / "03-run.png"), full_page=True)
    body = text_of(page)
    check("run page shows stages", all(w in body for w in ("normalize", "compare", "aggregate")),
          body[:160].replace("\n", " "))
    check("run page shows the outcome numbers",
          "502" in body and "385" in body, "502/385 in page text")

    # ---- 3. check report ------------------------------------------------
    page.goto(f"{BASE}/runs/{run_id}/report", wait_until="networkidle")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(SHOTS / "04-report.png"), full_page=True)
    body = text_of(page)
    check("report lists delivered checks", "Approval Rate" in body,
          body[:200].replace("\n", " "))
    check("report shows CONFIDENCE", "HIGH" in body or "LOW" in body)
    check("report offers HTML download", "HTML" in body)

    # ---- 4. pivot -------------------------------------------------------
    page.goto(f"{BASE}/runs/{run_id}/explore", wait_until="networkidle")
    page.wait_for_timeout(3500)
    page.screenshot(path=str(SHOTS / "05-explore.png"), full_page=True)
    body = text_of(page)
    check("explore page renders a pivot", "PATH" in body or "KEY" in body,
          body[:160].replace("\n", " "))

    # ---- 5. drill-down: the cell counts are flat buttons -----------------
    cells = page.locator("button")
    clicked_cell = False
    for i in range(min(cells.count(), 400)):
        c = cells.nth(i)
        try:
            txt = (c.inner_text() or "").strip().replace("\u00a0", " ").replace(" ", "")
        except Exception:  # noqa: BLE001
            continue
        if txt.isdigit() and 0 < int(txt) <= 2002:
            c.click()
            clicked_cell = True
            print(f"  clicked pivot cell with value {txt}")
            break
    page.wait_for_timeout(3000)
    page.screenshot(path=str(SHOTS / "06-drill.png"), full_page=True)
    body = text_of(page)
    check("drill-down produced an application list",
          clicked_cell and ("Уникальных заявок" in body) and ("I3" in body),
          "clicked" if clicked_cell else "no clickable numeric cell found")

    # ---- 6. application card: attributes before/after --------------------
    app_link = page.locator("a", has_text="I3").first
    if app_link.count():
        href = app_link.get_attribute("href")
        page.goto(BASE + href, wait_until="networkidle")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "07-app.png"), full_page=True)
        body = text_of(page)
        check("application card shows attributes", "BEFORE" in body or "ДО" in body,
              body[:160].replace("\n", " "))
        check("application card offers raw JSON", "JSON" in body, "")
    else:
        check("application card link present", False, "no app link after drill")

    check("no uncaught JS errors", not errors, "; ".join(errors[:3]))
    browser.close()

bad = [r for r in ok if not r[1]]
print("\n" + "=" * 60)
print(f"{len(ok) - len(bad)}/{len(ok)} browser checks passed")
print("screenshots:", SHOTS)
for name, _, detail in bad:
    print("FAILED:", name, detail)
sys.exit(0 if not bad else 2)

sys.exit(1 if bad else 0)
