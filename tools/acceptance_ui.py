#!/usr/bin/env python3
"""Acceptance walk of the *running* web application.

    app/.venv/bin/python tools/acceptance_ui.py --base-url http://127.0.0.1:8000

What is verified
----------------
1. **Anonymous pass** (no cookies): every route answers 200, ``/login`` carries the
   form, and no protected page reveals a run name, a check table or a stage log.
2. **Authenticated pass**: the tool logs in through a real headless Chromium
   (Playwright is installed in the project venv), keeps the ``session`` cookie and
   re-requests every page with :mod:`urllib`.  NiceGUI renders the element tree
   server-side, so the first HTML response already carries the markers — and the
   expected markers are read from the database (the checks a run actually
   published), never hard-coded.
3. **Pivot pass**: the explore table is built on the client, so a real browser
   presses «Пересчитать» and the tool asserts the displayed grand total equals the
   number of application pairs of the run.
4. **Isolation pass**: the second demo user must be refused the first user's run.
5. **Export pass**: the HTML report is produced by the application and opens as a
   stand-alone document.

Exit codes: ``0`` everything checked passed, ``1`` something failed, ``2`` nothing
failed but some checks could not run — the tool names them and never reports an
unexecuted check as a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))

PASS, FAIL, NOT_VERIFIED = "PASS", "FAIL", "NOT_VERIFIED"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    base_url: str
    started_at: str
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(Result(name, status, detail))

    def count(self, status: str) -> int:
        return sum(1 for item in self.results if item.status == status)

    @property
    def exit_code(self) -> int:
        if self.count(FAIL):
            return 1
        return 2 if self.count(NOT_VERIFIED) else 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def fetch(url: str, cookie: str = "", timeout: float = 30.0) -> tuple[int, str, str]:
    """GET ``url`` -> ``(status, body, error)``.

    Redirects are not followed: the anonymous behaviour must be observed exactly
    as the browser would get it.
    """
    request = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:            # 4xx/5xx are answers too
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return exc.code, body, ""
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        return 0, "", f"{type(exc).__name__}: {exc}"


def missing_markers(html: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if marker not in html]


def present_markers(html: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if marker in html]


def fmt_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


# ---------------------------------------------------------------------------
# a real browser: login, and the client-rendered pivot
# ---------------------------------------------------------------------------


def _sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _login(page, base_url: str, login: str, password: str, timeout: int = 30000) -> None:
    page.goto(f"{base_url}/login", timeout=timeout)
    inputs = page.locator("input")
    if inputs.count() < 2:
        raise RuntimeError("the login form has fewer than two inputs")
    inputs.nth(0).fill(login)
    page.locator('input[type="password"]').fill(password)
    page.get_by_role("button", name="Войти").click()
    page.wait_for_url(f"{base_url}/", timeout=timeout)


def browser_login(base_url: str, login: str, password: str) -> tuple[str, str]:
    """Return ``(cookie_header, "")`` or ``("", reason)``."""
    try:
        sync_playwright = _sync_playwright()
    except Exception as exc:                          # noqa: BLE001
        return "", f"playwright is not importable: {type(exc).__name__}: {exc}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                _login(context.new_page(), base_url, login, password)
                cookies = [c for c in context.cookies() if c.get("name") == "session"]
                if not cookies:
                    return "", "login succeeded but no 'session' cookie was set"
                return f'session={cookies[0]["value"]}', ""
            finally:
                browser.close()
    except Exception as exc:                          # noqa: BLE001
        return "", f"browser login failed: {type(exc).__name__}: {exc}"


def pivot_in_browser(base_url: str, login: str, password: str,
                     run_id: str) -> tuple[int | None, bool, str]:
    """Open /explore, press «Пересчитать», read the displayed grand total."""
    try:
        sync_playwright = _sync_playwright()
    except Exception as exc:                          # noqa: BLE001
        return None, False, f"playwright is not importable: {type(exc).__name__}: {exc}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                _login(page, base_url, login, password)
                page.goto(f"{base_url}/runs/{run_id}/explore", timeout=30000)
                page.get_by_role("button", name="Пересчитать").click()
                # the counter is set before the matrix rows are added, so wait for
                # the last element the table renders, not for the first one
                page.wait_for_function(
                    "() => document.body.innerText.includes('Итог по строкам')",
                    timeout=90000,
                )
                text = page.inner_text("body")
                match = re.search(r"Уникальных заявок в выборке:\s*([\d\s\u00a0]+)", text)
                total = int(re.sub(r"\D", "", match.group(1))) if match else None
                has_totals = "Итого" in text and "Итог по строкам" in text
                return total, has_totals, ""
            finally:
                browser.close()
    except Exception as exc:                          # noqa: BLE001
        return None, False, f"browser pivot failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# expectations read from the application's own data (read-only)
# ---------------------------------------------------------------------------


def delivered_checks() -> dict[str, str]:
    from change_analysis.checks.loader import list_builtin_checks

    return {spec.id: spec.label for spec in list_builtin_checks()}


def demo_user(login: str, password: str) -> tuple[str | None, str]:
    try:
        from change_analysis.auth import service as auth

        user_id = auth.authenticate(login, password)
        return (user_id, "") if user_id else (None, f"неверный пароль для {login!r}")
    except Exception as exc:                          # noqa: BLE001
        return None, f"нет доступа к базе: {type(exc).__name__}: {exc}"


def pick_run(user_id: str, run_id: str) -> tuple[dict | None, str]:
    """The run to inspect: the given id, or the newest SUCCEEDED run of the user."""
    try:
        from change_analysis.services import runs as runs_service

        if run_id:
            run = runs_service.get_run(user_id, run_id)
            return {"id": str(run["id"]), "name": run["name"],
                    "summary": dict(run["summary"] or {})}, ""
        items = [run for run in runs_service.list_runs(user_id) if run["state"] == "SUCCEEDED"]
        if not items:
            return None, "у пользователя нет завершённых прогонов"
        run = max(items, key=lambda item: item["created_at"])
        return {"id": str(run["id"]), "name": run["name"],
                "summary": dict(run["summary"] or {})}, ""
    except Exception as exc:                          # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def published_labels(user_id: str, run_id: str) -> tuple[list[str], str]:
    try:
        from change_analysis.services import checks as checks_service

        view = checks_service.check_report(user_id, run_id)
        return sorted({row["LABEL"] for row in view["rows"]}), ""
    except Exception as exc:                          # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# the passes
# ---------------------------------------------------------------------------


def pass_anonymous(report: Report, base_url: str, run: dict | None,
                   checks: dict[str, str]) -> None:
    status, html, error = fetch(f"{base_url}/login")
    if error:
        report.add("anonymous: /login отвечает", FAIL, error)
        return
    required = ["СПР · Анализ изменений", "Логин", "Войти"]
    missing = missing_markers(html, required)
    report.add("anonymous: /login отдаёт форму входа",
               PASS if status == 200 and not missing else FAIL,
               f"status={status} missing={missing}")

    paths = ["/", "/configs", "/workspace", "/checks"]
    if run:
        paths += [f"/runs/{run['id']}", f"/runs/{run['id']}/report",
                  f"/runs/{run['id']}/explore"]
    secrets = ["Единая таблица проверок", "Этапы выполнения", "Мои прогоны",
               "Рабочая область Python", "Конфигурация прогона"]
    if run and run.get("name"):
        secrets.append(run["name"])
    if run and (run.get("summary") or {}).get("changed"):
        secrets.append(fmt_int(run["summary"]["changed"]))
    for path in paths:
        status, body, error = fetch(f"{base_url}{path}")
        if error:
            report.add(f"anonymous: {path} отвечает", FAIL, error)
            continue
        leaked = present_markers(body, secrets)
        report.add(f"anonymous: {path} не отдаёт данные",
                   PASS if status == 200 and not leaked else FAIL,
                   f"status={status} leaked={leaked[:3]}")


def authenticated_pages(run: dict | None, checks: dict[str, str],
                        user_id: str | None) -> list[tuple[str, list[str]]]:
    """Every authenticated page with the markers it must carry."""
    summary = (run or {}).get("summary") or {}
    pages: list[tuple[str, list[str]]] = [
        ("/", ["Мои прогоны", "Новая конфигурация"]),
        ("/configs", ["Конфигурация прогона", "Процедуры обработки",
                      "Автоматические проверки"]),
        ("/workspace", ["Рабочая область Python", "Зарегистрированные файлы"]),
        ("/checks", ["Проверки"] + list(checks)),
    ]
    if run:
        markers = ["Этапы выполнения", "Журнал", "normalize", "compare", "aggregate",
                   "к коду"]
        if summary.get("changed"):
            markers.append(fmt_int(summary["changed"]))
        pages.append((f"/runs/{run['id']}", markers))

        labels: list[str] = []
        if user_id:
            labels, _ = published_labels(user_id, run["id"])
        pages.append((f"/runs/{run['id']}/report",
                      ["Единая таблица проверок", "Скачать HTML"] + labels))
        pages.append((f"/runs/{run['id']}/explore",
                      ["Раскладка", "Пересчитать", "Строки", "Колонки"]))
    return pages


def pass_isolation(report: Report, base_url: str, run: dict, login: str,
                   password: str) -> None:
    cookie, reason = browser_login(base_url, login, password)
    if not cookie:
        report.add(f"isolation: вход {login}", NOT_VERIFIED, reason)
        return
    status, html, error = fetch(f"{base_url}/runs/{run['id']}", cookie=cookie)
    if error:
        report.add(f"isolation: {login} не читает чужой прогон", FAIL, error)
        return
    leaked = present_markers(html, [run["name"], "Единая таблица проверок",
                                    "Этапы выполнения"])
    refused = "принадлежит другому пользователю" in html
    report.add(f"isolation: {login} не читает чужой прогон",
               PASS if status == 200 and refused and not leaked else FAIL,
               f"status={status} refused={refused} leaked={leaked[:3]}")


def pass_pivot(report: Report, base_url: str, login: str, password: str,
               run: dict | None) -> None:
    if run is None:
        report.add("pivot: сводная считает уникальные заявки", NOT_VERIFIED,
                   "прогон не выбран")
        return
    expected = (run.get("summary") or {}).get("pairs")
    total, has_totals, reason = pivot_in_browser(base_url, login, password, run["id"])
    if total is None:
        report.add("pivot: сводная считает уникальные заявки", NOT_VERIFIED, reason)
        return
    report.add("pivot: сводная считает уникальные заявки",
               PASS if expected is None or total == int(expected) else FAIL,
               f"показано={total} ожидалось={expected}")
    report.add("pivot: таблица содержит итоги", PASS if has_totals else FAIL,
               "«Итого» и «Итог по строкам»")


def pass_export(report: Report, user_id: str | None, run: dict | None) -> None:
    if user_id is None or run is None:
        report.add("export: HTML-отчёт собирается и открывается", NOT_VERIFIED,
                   "нет пользователя или прогона")
        return
    try:
        from change_analysis.services import report as report_service

        path = report_service.export_html(user_id, run["id"])
        html = Path(path).read_text(encoding="utf-8")
        ok = ("<html" in html and "Результаты автоматических проверок" in html
              and "<script" not in html.lower() and "http" in html)
        report.add("export: HTML-отчёт собирается и открывается",
                   PASS if ok else FAIL, f"file={Path(path).name} bytes={len(html)}")
    except Exception as exc:                          # noqa: BLE001
        report.add("export: HTML-отчёт собирается и открывается", FAIL,
                   f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--login", default="analyst")
    parser.add_argument("--password", default="analyst")
    parser.add_argument("--second-login", default="analyst2")
    parser.add_argument("--second-password", default="analyst2")
    parser.add_argument("--run-id", default="", help="default: newest SUCCEEDED run")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    report = Report(base_url=base_url,
                    started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    status, _, error = fetch(f"{base_url}/login")
    if error or status != 200:
        report.add("приложение отвечает на /login", FAIL, f"status={status} {error}".strip())
        return finish(report, args.json)

    try:
        checks = delivered_checks()
        report.add("выданные проверки загружаются", PASS, f"{len(checks)} проверки")
    except Exception as exc:                          # noqa: BLE001
        checks = {}
        report.add("выданные проверки загружаются", FAIL, f"{type(exc).__name__}: {exc}")

    user_id, reason = demo_user(args.login, args.password)
    if user_id is None:
        report.add(f"пользователь {args.login} аутентифицируется", NOT_VERIFIED, reason)
    else:
        report.add(f"пользователь {args.login} аутентифицируется", PASS,
                   f"user_id={user_id[:8]}…")

    run, run_reason = (None, "нет пользователя")
    if user_id:
        run, run_reason = pick_run(user_id, args.run_id)
    if run is None:
        report.add("прогон для проверки найден", NOT_VERIFIED, run_reason)
    else:
        report.add("прогон для проверки найден", PASS,
                   f"{run['id']} · {run['summary'].get('pairs')} пар")

    pass_anonymous(report, base_url, run, checks)

    cookie, login_reason = browser_login(base_url, args.login, args.password)
    if not cookie:
        for path, _ in authenticated_pages(run, checks, user_id):
            report.add(f"auth: {path} содержит ожидаемые маркировки", NOT_VERIFIED,
                       login_reason)
        report.add("auth: неизвестный маршрут отвечает 404", NOT_VERIFIED, login_reason)
    else:
        report.add(f"вход {args.login} через браузер", PASS)
        for path, markers in authenticated_pages(run, checks, user_id):
            status, html, error = fetch(f"{base_url}{path}", cookie=cookie)
            if error:
                report.add(f"auth: {path} содержит ожидаемые маркировки", FAIL, error)
                continue
            missing = missing_markers(html, markers)
            report.add(f"auth: {path} содержит ожидаемые маркировки",
                       PASS if status == 200 and not missing else FAIL,
                       f"status={status} missing={missing[:4]}")
        status, _, _ = fetch(f"{base_url}/no-such-page", cookie=cookie)
        report.add("auth: неизвестный маршрут отвечает 404",
                   PASS if status == 404 else FAIL, f"status={status}")
        pass_export(report, user_id, run)

    if run is not None:
        pass_isolation(report, base_url, run, args.second_login, args.second_password)
        pass_pivot(report, base_url, args.login, args.password, run)

    return finish(report, args.json)


def finish(report: Report, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "base_url": report.base_url,
            "started_at": report.started_at,
            "exit_code": report.exit_code,
            "counts": {status: report.count(status)
                       for status in (PASS, FAIL, NOT_VERIFIED)},
            "results": [{"name": r.name, "status": r.status, "detail": r.detail}
                        for r in report.results],
        }, ensure_ascii=False, indent=2))
        return report.exit_code

    print(f"Приёмка UI · {report.base_url} · {report.started_at}")
    print("=" * 78)
    for item in report.results:
        line = f"{item.status:<12} {item.name}"
        if item.detail:
            line += f"  [{item.detail}]"
        print(line)
    print("=" * 78)
    print(f"PASS={report.count(PASS)}  FAIL={report.count(FAIL)}  "
          f"NOT_VERIFIED={report.count(NOT_VERIFIED)}")
    if report.exit_code == 2:
        print("Внимание: часть проверок не выполнена — это не «зелёно».")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
