"""Configuration editor: dataset, three procedures (two sources each), checks."""

from __future__ import annotations

import json
import uuid
from typing import Any

from nicegui import ui

from ..services import configs, datasets, procedures, runs
from . import layout, session

STAGE_LABEL = {
    "normalize": "Нормализация",
    "compare": "Сравнение",
    "aggregate": "Агрегация",
}
SOURCE_LABEL = {"file": "Файл .py", "workspace": "Рабочая область"}


@ui.page("/configs")
def configs_page(config_id: str | None = None) -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="configs")
    owner = session.user_id(user)
    options = configs.configuration_form_options(owner)

    existing = configs.list_configurations(owner)
    current = None
    if config_id:
        try:
            current = configs.load_configuration(owner, config_id)
        except Exception:  # noqa: BLE001 - a stale link must not break the page
            ui.notify("Конфигурация не найдена", type="negative")

    selections = (current or {}).get("selections") or configs.default_selections(owner)

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Конфигурация прогона").classes("text-xl font-semibold")
            ui.space()
            if existing:
                picker = ui.select(
                    {str(c["id"]): c["name"] for c in existing},
                    label="Открыть сохранённую",
                    on_change=lambda e: ui.navigate.to(f"/configs?config_id={e.value}"),
                    value=str(current["id"]) if current else None,
                ).props("outlined dense").classes("w-64")

        name_input = ui.input("Название конфигурации", value=(current or {}).get("name", "")) \
            .props("outlined dense").classes("w-full")

        dataset_list = options["datasets"]
        if not dataset_list:
            layout.error_box(
                "Нет зарегистрированных датасетов. Зарегистрируйте прогон на странице «Прогоны»."
            )
            return
        dataset_select = ui.select(
            {str(d["id"]): f"{d['name']} ({d['app_pairs']} пар)" for d in dataset_list},
            label="Датасет",
            value=str(current["dataset_id"]) if current and current.get("dataset_id")
            else str(dataset_list[0]["id"]),
        ).props("outlined dense").classes("w-full")
        precheck_box = ui.column().classes("w-full gap-1")

        def refresh_precheck() -> None:
            precheck_box.clear()
            dataset_id = dataset_select.value
            if not dataset_id:
                return
            try:
                dataset = datasets.get_dataset(owner, str(dataset_id))
            except Exception as exc:  # noqa: BLE001
                with precheck_box:
                    layout.error_box(f"Датасет недоступен: {exc}")
                return
            check = datasets.precheck_dataset(dataset["root_ref"])
            with precheck_box:
                with ui.row().classes("items-center gap-2 text-sm"):
                    ui.icon("check_circle" if check["ok"] else "error",
                            color="green" if check["ok"] else "red")
                    ui.label(f"Пар заявок: {check['pairs']} "
                             f"(BEFORE {check.get('before', '—')} / AFTER {check.get('after', '—')})")
                    if check.get("declared"):
                        ui.label(f"manifest: {check['declared']}").classes("text-slate-500")
                for error in check["errors"]:
                    ui.label("⚠ " + error).classes("text-red-600 text-xs")
                for warning in check["warnings"]:
                    ui.label("• " + warning).classes("text-orange-600 text-xs")
                if check.get("technical_fields"):
                    ui.label("Исключаемые технические поля: "
                             + ", ".join(check["technical_fields"])).classes(
                        "text-xs text-slate-500")

        dataset_select.on_value_change(lambda _: refresh_precheck())
        refresh_precheck()

        # -- procedures -----------------------------------------------------
        procedure_state: dict[str, dict[str, Any]] = {}
        procedure_params: dict[str, Any] = {}
        with ui.card().classes("w-full"):
            ui.label("Процедуры обработки").classes("text-lg font-semibold")
            ui.label("Каждый этап — отдельный Python-скрипт. Источник: рабочая "
                     "область или зарегистрированный файл .py.").classes(
                "text-xs text-slate-500")
            for stage in procedures.PROCEDURE_STAGES:
                selection = ((selections.get("procedures") or {}).get(stage)
                             or procedures.default_selection(stage))
                procedure_state[stage] = dict(selection)
                with ui.row().classes("w-full items-center gap-3 border-t pt-3 mt-2"):
                    ui.label(STAGE_LABEL[stage]).classes("w-32 font-medium")
                    source_select = ui.toggle(
                        {"file": SOURCE_LABEL["file"], "workspace": SOURCE_LABEL["workspace"]},
                        value=selection.get("source", "file"),
                    ).props("dense")
                    file_select = ui.select(
                        {f["path"]: f["path"] for f in options["registered_files"]},
                        label="Зарегистрированный файл",
                        value=selection.get("ref") or procedures.BUILTIN_FILES[stage][1],
                    ).props("outlined dense").classes("w-64")
                    own = options["procedures"].get(stage) or []
                    workspace_select = ui.select(
                        {str(p["id"]): p["name"] for p in own},
                        label="Моя процедура", value=selection.get("procedure_id"),
                    ).props("outlined dense").classes("w-56")
                    params_input = ui.input(
                        "Параметры (JSON)", value=json.dumps(selection.get("params") or {})
                    ).props("outlined dense").classes("flex-grow")
                    procedure_params[stage] = params_input

                    def bind(stage=stage, source=source_select, file_sel=file_select,
                             ws=workspace_select, params=params_input) -> None:
                        def update() -> None:
                            state = procedure_state[stage]
                            state["source"] = source.value
                            state["ref"] = file_sel.value
                            state["procedure_id"] = ws.value
                            try:
                                state["params"] = json.loads(params.value or "{}")
                            except json.JSONDecodeError:
                                pass
                            file_sel.set_visibility(source.value == "file")
                            ws.set_visibility(source.value == "workspace")
                        source.on_value_change(lambda _: update())
                        file_sel.on_value_change(lambda _: update())
                        ws.on_value_change(lambda _: update())
                        params.on_value_change(lambda _: update())
                        file_sel.set_visibility(source.value == "file")
                        ws.set_visibility(source.value == "workspace")
                        update()

                    bind()

        # -- checks ---------------------------------------------------------
        check_selection: dict[str, dict[str, Any]] = {
            str(c.get("id")): dict(c) for c in (selections.get("checks") or [])
        }
        with ui.card().classes("w-full"):
            ui.label("Автоматические проверки").classes("text-lg font-semibold")
            ui.label("Выданные проверки, зарегистрированные файлы и собственный код "
                     "исполняются в одном отчёте.").classes("text-xs text-slate-500")
            switches: dict[str, Any] = {}
            param_inputs: dict[str, Any] = {}
            for check in options["checks"]:
                check_id = str(check["id"])
                with ui.row().classes("w-full items-center gap-3"):
                    switch = ui.switch(check["label"], value=check_id in check_selection)
                    ui.label(f"id: {check_id}").classes("text-xs text-slate-400")
                    param_input = ui.input(
                        "параметры",
                        value=json.dumps(
                            (check_selection.get(check_id) or {}).get("params")
                            or check.get("params") or {}
                        ),
                    ).props("outlined dense").classes("flex-grow")
                    switches[check_id] = switch
                    param_inputs[check_id] = param_input
                    if check.get("source") == "workspace":
                        check_selection.setdefault(
                            check_id, {"id": check_id, "source": "workspace",
                                       "procedure_id": check.get("procedure_id")})

        # -- backend --------------------------------------------------------
        backend_toggle = ui.toggle(
            {"cpu": "CPU (pandas)", "gpu": "GPU (cudf.pandas)"},
            value=(current or {}).get("params", {}).get("backend", "cpu"),
        ).props("dense")
        ui.label("GPU-режим доступен только если он настроен на этой установке; "
                 "иначе запуск честно откажет, а не выполнится молча на CPU.").classes(
            "text-xs text-slate-500")

        status = ui.label("").classes("text-sm")

        saved = {"id": str(current["id"]) if current else None}

        def parse_params(value: str, label: str) -> dict[str, Any]:
            try:
                result = json.loads(value or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label}: некорректный JSON") from exc
            if not isinstance(result, dict):
                raise ValueError(f"{label}: ожидается JSON-объект")
            return result

        def collect() -> dict[str, Any]:
            for stage, field in procedure_params.items():
                procedure_state[stage]["params"] = parse_params(field.value, STAGE_LABEL[stage])
            chosen_checks = []
            for check_id, switch in switches.items():
                if not switch.value:
                    continue
                item = dict(check_selection.get(check_id) or {"id": check_id})
                item["params"] = parse_params(param_inputs[check_id].value, check_id)
                chosen_checks.append(item)
            return {"procedures": procedure_state, "checks": chosen_checks}

        def save() -> str | None:
            try:
                new_id = configs.save_configuration(
                    owner, name_input.value or "Конфигурация",
                    str(dataset_select.value), collect(),
                    {"backend": backend_toggle.value},
                    config_id=saved["id"],
                )
            except Exception as exc:  # noqa: BLE001
                status.set_text(f"Не сохранено: {exc}")
                ui.notify(str(exc), type="negative")
                return None
            saved["id"] = new_id
            ui.run_javascript(f"history.replaceState(null, '', '/configs?config_id={new_id}')")
            status.set_text("Конфигурация сохранена")
            ui.notify("Конфигурация сохранена", type="positive")
            return new_id

        def run_now() -> None:
            try:
                new_id = save()
                if new_id is None:
                    return
                selections_now = collect()
                run_id = runs.create_run(
                    owner, name_input.value or "Прогон", str(dataset_select.value),
                    selections_now, {"backend": backend_toggle.value},
                    backend=str(backend_toggle.value),
                    idempotency_key=uuid.uuid4().hex,
                )
            except Exception as exc:  # noqa: BLE001
                status.set_text(f"Запуск не удался: {exc}")
                ui.notify(str(exc), type="negative")
                return
            ui.navigate.to(f"/runs/{run_id}")

        with ui.row().classes("gap-3"):
            ui.button("Сохранить", on_click=save).props("outline")
            ui.button("Сохранить и запустить", on_click=run_now).props("unelevated")
