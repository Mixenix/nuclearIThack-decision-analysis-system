"""Timed rehearsal of the 20-minute defence scenario, executed in a real browser.

Follows docs/DEMO.md block by block and prints the wall-clock cost of each block,
so the plan can be checked against the 20-minute budget instead of being assumed.

    cd app && .venv/bin/python tools/dev/rehearse_demo.py [run_id]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screens"
RUN = sys.argv[1] if len(sys.argv) > 1 else ""

blocks: list[tuple[str, float, bool, str]] = []


class Block:
    def __init__(self, title: str) -> None:
        self.title = title

    def __enter__(self):
        self.t0 = time.perf_counter()
        print(f"\n— {self.title}")
        return self

    def __exit__(self, exc_type, exc, tb):
        spent = time.perf_counter() - self.t0
        ok = exc_type is None
        blocks.append((self.title, spent, ok, "" if ok else f"{exc_type.__name__}: {exc}"))
        print(f"   {'ok' if ok else 'ПАДЕНИЕ'} · {spent:.1f} с")
        return False  # let the exception surface


def main() -> int:
    total0 = time.perf_counter()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100}, locale="ru-RU")
        page = ctx.new_page()
        page.set_default_timeout(25000)

        with Block("0–3 мин · вход и конфигурация"):
            page.goto(f"{BASE}/login")
            page.wait_for_timeout(1200)
            inputs = page.locator("input")
            inputs.nth(0).fill("analyst")
            inputs.nth(1).fill("analyst")
            page.get_by_role("button", name="Войти").first.click()
            page.wait_for_timeout(1600)
            page.goto(f"{BASE}/configs")
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
            assert "Входной формат" in body, "на странице конфигурации нет выбора формата"
            assert "2 002" in body or "2002" in body, "не видно предпроверки датасета"
            toggles = page.locator(".q-toggle")
            for k in range(toggles.count()):
                if (toggles.nth(k).get_attribute("aria-checked") or "") != "true":
                    toggles.nth(k).click()
                    page.wait_for_timeout(80)
            page.screenshot(path=str(SHOTS / "13-config.png"), full_page=True)

        with Block("3–6 мин · запуск и журнал этапов"):
            page.get_by_role("button", name="Сохранить и запустить").click()
            for _ in range(30):
                page.wait_for_timeout(1000)
                if "/runs/" in page.url:
                    break
            assert "/runs/" in page.url, "запуск не привёл на страницу прогона"
            run_id = page.url.split("/runs/")[1].split("/")[0].split("?")[0]
            print(f"   run_id={run_id}")
            # the log is live: stages appear one after another, which is exactly
            # what the demo shows.  Wait for all three with a generous budget and
            # report how long the log took to grow.
            t_log = time.perf_counter()
            stage_text = ""
            while time.perf_counter() - t_log < 120:
                stage_text = page.inner_text("body")
                if all(w in stage_text for w in ("normalize", "compare", "aggregate")):
                    break
                page.wait_for_timeout(1000)
            print(f"   все три этапа в журнале через {time.perf_counter() - t_log:.1f} с")
            assert all(w in stage_text for w in ("normalize", "compare", "aggregate")), \
                "в журнале нет трёх этапов"
            assert page.get_by_role("button", name="к коду").count() > 0, "нет перехода к коду"
            page.screenshot(path=str(SHOTS / "03-run.png"), full_page=True)

        with Block("6–7 мин · числа (ожидание завершения)"):
            text = ""
            for _ in range(180):
                page.wait_for_timeout(2000)
                text = page.inner_text("body")
                if "502" in text and "385" in text:
                    break
            assert "502" in text and "385" in text, "прогон не дошёл до чисел 502/385"
            page.screenshot(path=str(SHOTS / "14-run-from-ui.png"), full_page=True)

        with Block("7–10 мин · сводная и смена раскладки"):
            page.goto(f"{BASE}/runs/{run_id}/explore")
            page.wait_for_timeout(3000)
            assert "Уникальных заявок" in page.inner_text("body"), "сводная не открылась"
            page.get_by_role("button", name="Пересчитать").click()
            page.wait_for_timeout(2500)
            page.screenshot(path=str(SHOTS / "05-explore.png"), full_page=True)

        with Block("10–13 мин · drill-down и карточка заявки"):
            cells = page.locator("button")
            clicked = False
            for i in range(min(cells.count(), 400)):
                label = (cells.nth(i).inner_text() or "").strip().replace("\u00a0", "").replace(" ", "")
                if label.isdigit() and 0 < int(label) <= 2002:
                    cells.nth(i).click()
                    clicked = True
                    break
            assert clicked, "не нашлось кликабельной ячейки"
            page.wait_for_timeout(2500)
            assert "Уникальных заявок" in page.inner_text("body"), "drill-down не показал список"
            link = page.locator("a", has_text="I3").first
            assert link.count(), "в drill-down нет ссылок на заявки"
            page.goto(BASE + link.get_attribute("href"))
            page.wait_for_timeout(2500)
            assert "JSON" in page.inner_text("body"), "в карточке заявки нет исходного JSON"
            page.screenshot(path=str(SHOTS / "07-app.png"), full_page=True)

        with Block("13–15 мин · отчёт проверок и мнение ИИ"):
            page.goto(f"{BASE}/runs/{run_id}/report")
            page.wait_for_timeout(3000)
            rep = page.inner_text("body")
            assert "Единая таблица проверок" in rep, "нет единой таблицы проверок"
            assert "Независимое мнение ИИ" in rep, "нет блока ИИ"
            page.screenshot(path=str(SHOTS / "04-report.png"), full_page=True)

        with Block("15–17 мин · новая проверка в рабочей области"):
            page.goto(f"{BASE}/workspace")
            page.wait_for_timeout(2500)
            page.get_by_role("button", name="НОВАЯ ПРОВЕРКА").click()
            page.wait_for_timeout(2000)
            name = f"ДЕМО {time.strftime('%H:%M:%S')}"
            page.get_by_label("Название", exact=True).fill(name)
            editor = page.locator(".cm-content")
            code = "\n".join(editor.locator(".cm-line").all_text_contents())
            code = code.replace("Одобренные заявки ушли в отказ", f"ДЕМО {name}")
            editor.fill(code)
            page.get_by_role("button", name="Проверить контракт").click()
            page.wait_for_timeout(2500)
            page.get_by_role("button", name="Сохранить").click()
            page.wait_for_timeout(2500)
            combo = page.get_by_role("combobox", name="Готовый прогон")
            combo.click()
            page.wait_for_timeout(700)
            page.get_by_role("option").first.click()
            page.wait_for_timeout(500)
            page.get_by_role("button", name="Выполнить на прогоне").click()
            page.wait_for_timeout(2000)
            page.screenshot(path=str(SHOTS / "11-new-check.png"), full_page=True)

        with Block("17–18 мин · HTML"):
            page.goto(f"{BASE}/runs/{run_id}/report")
            page.wait_for_timeout(2500)
            with page.expect_download() as dl:
                page.get_by_role("button", name="СКАЧАТЬ HTML").click()
            download = dl.value
            target = Path("/tmp") / (download.suggested_filename or "report.html")
            download.save_as(target)
            html = target.read_text(encoding="utf-8")
            assert len(html) > 20000, "скачанный HTML подозрительно мал"
            assert "<style" in html and "cdn" not in html.lower(), "HTML не автономен"
            print(f"   скачано {len(html)} символов → {target}")

        with Block("18–20 мин · второй пользователь"):
            other = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
            other.goto(f"{BASE}/login")
            other.wait_for_timeout(1200)
            fields = other.locator("input")
            fields.nth(0).fill("analyst2")
            fields.nth(1).fill("analyst2")
            other.get_by_role("button", name="Войти").first.click()
            other.wait_for_timeout(1600)
            other.goto(f"{BASE}/runs/{run_id}")
            other.wait_for_timeout(2500)
            txt = other.inner_text("body")
            assert "другому пользователю" in txt or "принадлежит" in txt, \
                "второй пользователь не получил отказа на чужой прогон"
            other.screenshot(path=str(SHOTS / "16-isolation.png"), full_page=True)

        browser.close()

    total = time.perf_counter() - total0
    print("\n" + "=" * 64)
    print(f"{'блок':46s} {'секунд':>8s}  статус")
    for title, spent, ok, err in blocks:
        print(f"{title[:46]:46s} {spent:8.1f}  {'ok' if ok else 'ПАДЕНИЕ'}")
    print(f"{'ИТОГО':46s} {total:8.1f}")
    budget = 20 * 60
    print(f"\nбюджет 20 мин = {budget} с; запас {budget - total:+.0f} с "
          f"({(total / budget) * 100:.0f} % бюджета)")
    failed = [b for b in blocks if not b[2]]
    if failed:
        print("\nПРОВАЛЫ:")
        for title, _, _, err in failed:
            print(f"  {title}: {err}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
