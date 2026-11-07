"""Python workspace: edit a procedure or a check, save it, apply it.

Two sources for one contract: the editor writes text into the database, the file
picker points at a registered ``.py``.  Execution always happens in the worker —
never inside the web process (PLAN.md §11.3).
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import run as background, ui

from ..checks.loader import list_builtin_checks
from ..services import procedures, report as report_service, runs
from ..services import workspace as workspace_service
from . import workspace_examples
from . import layout, session

CHECK_TEMPLATE = '''# -*- coding: utf-8 -*-
"""Своя проверка. Контракт: CHECK = {...}; run(mart, params) -> list[dict]."""

DEFAULTS = {"app_ids_limit": 20, "low_confidence_cnt": 5}


def run(mart, params=None):
    p = {**DEFAULTS, **(params or {})}
    total = mart.total_apps()
    apps = set()
    for row in mart.rows(path="decisionResult.decision", change_type="VALUE_CHANGED"):
        if row["VALUE_AFTER"] == "REJECT":
            apps.add(row["APP_ID"])
    return [{
        "LABEL": CHECK["label"],
        "SOURCE": "AFTER",
        "ARGUMENT": "decisionResult.decision",
        "CNT": len(apps),
        "TOTAL": total,
        "METRIC": round(len(apps) / total, 4) if total else 0.0,
        "APP_IDS": sorted(apps)[:p["app_ids_limit"]],
        "RESULT": "FAIL" if apps else "PASS",
        "CONFIDENCE": "LOW" if 0 < len(apps) < p["low_confidence_cnt"] else "HIGH",
    }]


CHECK = {"id": "approve_to_reject",
         "label": "Одобренные заявки ушли в отказ",
         "params": DEFAULTS,
         "entrypoint": run}
'''


@ui.page("/workspace")
def workspace_page(procedure_id: str | None = None, stage: str | None = None,
                   file: str | None = None) -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="workspace")
    owner = session.user_id(user)

    own_procedures = procedures.list_procedures(owner)
    registered = procedures.list_registered_files()

    initial_code = ""
    initial_params = "{}"
    initial_name = "Новая проверка"
    initial_stage = "check"
    if procedure_id:
        try:
            row = procedures.get_procedure(owner, procedure_id)
            initial_code = row.get("code") or ""
            initial_params = json.dumps(row.get("params") or {}, ensure_ascii=False, indent=2)
            initial_name = row["name"]
            initial_stage = row["stage"]
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Процедура не найдена: {exc}", type="negative")
    elif file:
        try:
            initial_code = procedures.read_registered_file(file)
            initial_name = file
            initial_stage = stage or "check"
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Файл не найден: {exc}", type="negative")
    elif stage and stage in procedures.BUILTIN_FILES:
        try:
            initial_code = procedures.read_registered_file(procedures.BUILTIN_FILES[stage][1])
            initial_name = procedures.BUILTIN_FILES[stage][0]
            initial_stage = stage
        except Exception:  # noqa: BLE001
            pass
    else:
        initial_code = CHECK_TEMPLATE

    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
        ui.label("Рабочая область Python").classes("text-xl font-semibold")
        ui.label("Код выполняется в отдельном процессе-воркере. Проверка попадает "
                 "в тот же отчёт о прогоне.").classes("text-xs text-slate-500")

        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("w-72 shrink-0"):
                ui.label("Мои процедуры и проверки").classes("font-medium")
                if not own_procedures:
                    ui.label("Пока ничего не сохранено").classes("text-xs text-slate-500")
                for row in own_procedures:
                    with ui.row().classes("items-center gap-1 w-full"):
                        ui.link(f"[{row['stage']}] {row['name']}",
                                f"/workspace?procedure_id={row['id']}").classes("text-sm")
                ui.separator()
                ui.label("Зарегистрированные файлы").classes("font-medium")
                for item in registered:
                    ui.link(item["path"], f"/workspace?file={item['path']}").classes("text-xs")
                ui.separator()
                ui.button("Новая проверка", icon="add",
                          on_click=lambda: ui.navigate.to("/workspace")).props("outline dense")

            with ui.card().classes("flex-grow"):
                with ui.row().classes("w-full items-center gap-2"):
                    name_input = ui.input("Название", value=initial_name).props(
                        "outlined dense").classes("w-64")
                    stage_select = ui.select(
                        ["check", "normalize", "compare", "aggregate"],
                        label="Тип", value=initial_stage,
                    ).props("outlined dense").classes("w-40")
                    ui.space()
                    status_label = ui.label("").classes("text-xs text-slate-500")

                editor = ui.codemirror(initial_code, language="Python").classes("w-full") \
                    .style("height: 46vh; border: 1px solid #cbd5e1")
                params_input = ui.input("Параметры (JSON)", value=initial_params).props(
                    "outlined dense").classes("w-full")

                with ui.expansion("Готовые примеры", icon="lightbulb",
                                  value=False).classes("w-full"):
                    ui.label("Проверены запуском на демонстрационном прогоне "
                             "(2 002 пары, 502 различия, 385 смен решения). Нажмите, "
                             "чтобы подставить в редактор, дальше меняйте как свой."
                             ).classes("text-xs text-slate-500 mb-1")

                    def apply_example(example) -> None:
                        editor.value = example["code"]
                        stage_select.value = example["stage"]
                        params_input.value = json.dumps(example["params"],
                                                        ensure_ascii=False)
                        if not (name_input.value or "").strip():
                            name_input.value = example["title"].split(": ", 1)[-1]
                        status_label.set_text(f"пример: {example['title']} — не забудьте сохранить")

                    for example in workspace_examples.EXAMPLES:
                        with ui.row().classes("items-start gap-2 w-full no-wrap"):
                            ui.button(example["title"],
                                      on_click=lambda e=example: apply_example(e)) \
                                .props("outline dense").classes("shrink-0")
                            ui.label(example["about"]).classes("text-xs text-slate-500")

                with ui.row().classes("gap-2 items-center"):
                    ui.button("Проверить контракт", icon="rule",
                              on_click=lambda: validate_contract()).props("outline")
                    ui.button("Сохранить", icon="save",
                              on_click=lambda: save()).props("unelevated")
                    ui.button("Выполнить на прогоне", icon="play_arrow",
                              on_click=lambda: execute_on_run()).props("outline")
                    finished_runs = [r for r in runs.list_runs(owner) if r["state"] == "SUCCEEDED"]
                    run_select = ui.select(
                        {str(r["id"]): f"{r['name']} · {layout.fmt_time(r['created_at'])}"
                         for r in finished_runs},
                        label="Готовый прогон",
                        value=str(finished_runs[0]["id"]) if finished_runs else None,
                    ).props("outlined dense").classes("flex-grow")

                output = ui.column().classes("w-full gap-1 mt-2")

    saved: dict[str, Any] = {"id": procedure_id if procedure_id and not file else None}

    def current_params() -> dict[str, Any]:
        try:
            value = json.loads(params_input.value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Параметры: некорректный JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Параметры: ожидается JSON-объект")
        return value

    def validate_contract() -> None:
        output.clear()
        # Никакого импорта и исполнения: только разбор AST (см. services/workspace.py).
        verdict = workspace_service.validate_code(editor.value or "", stage_select.value or "")
        with output:
            if verdict["ok"]:
                ui.label(f"Контракт в порядке: {verdict['message']}").classes(
                    "text-green-700 text-sm")
                if verdict.get("hint"):
                    ui.label(verdict["hint"]).classes("text-xs text-slate-500")
            else:
                layout.error_box(f"Контракт нарушен: {verdict['message']}")
                if verdict.get("hint"):
                    ui.label(verdict["hint"]).classes("text-xs text-slate-500")

    def save() -> str | None:
        try:
            new_id = procedures.save_procedure(
                owner, str(stage_select.value), name_input.value or "Без названия",
                "workspace", code=editor.value, params=current_params(),
                procedure_id=saved["id"],
            )
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Не сохранено: {exc}", type="negative")
            return None
        saved["id"] = new_id
        ui.run_javascript(f"history.replaceState(null, '', '/workspace?procedure_id={new_id}')")
        status_label.set_text("Сохранено")
        ui.notify("Сохранено", type="positive")
        return new_id

    async def execute_on_run() -> None:
        output.clear()
        if stage_select.value != "check":
            with output:
                ui.label("Процедуры этапов применяются через конфигурацию: сохраните код, "
                         "выберите его как источник «Рабочая область» и запустите прогон.") \
                    .classes("text-sm text-slate-600")
                ui.button("Перейти к конфигурации",
                          on_click=lambda: ui.navigate.to("/configs")).props("outline dense")
            return
        run_id = run_select.value
        if not run_id:
            ui.notify("Нужен готовый прогон", type="warning")
            return
        procedure = save()
        if not procedure:
            return
        try:
            previous = await background.io_bound(runs.get_run, owner, str(run_id))
            target_revision = int(previous["revision"]) + 1
            job_id = await background.io_bound(runs.request_checks_only,
                owner, str(run_id),
                [{"id": str(saved["id"]), "source": "workspace",
                  "procedure_id": str(saved["id"]), "params": current_params()}],
            )
        except Exception as exc:  # noqa: BLE001
            with output:
                layout.error_box(f"Не удалось поставить проверку в очередь: {exc}")
            return
        with output:
            waiting = ui.label(f"Проверка в очереди (job {job_id}). "
                               f"Ожидание публикации ревизии {target_revision}…")
            open_report = ui.button("Открыть отчёт", on_click=lambda: ui.navigate.to(
                f"/runs/{run_id}/report?revision={target_revision}")).props("outline dense")
            open_report.set_visibility(False)

            async def poll_publication() -> None:
                try:
                    view = await background.io_bound(
                        report_service.published_check_report, owner, str(run_id), target_revision)
                except Exception as exc:  # noqa: BLE001
                    waiting.set_text(f"Ожидание публикации ревизии {target_revision}: {exc}")
                    return
                waiting.set_text(f"Опубликована ревизия {view['revision']}"
                                 + ("; есть технические ошибки" if view["errors"] else ""))
                open_report.set_visibility(True)
                publication_timer.deactivate()

            publication_timer = ui.timer(1.5, poll_publication)

