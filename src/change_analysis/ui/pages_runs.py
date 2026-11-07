"""Login and the run list."""

from __future__ import annotations

from nicegui import app, ui

from ..auth import service as auth
from ..services import configs, datasets, runs
from . import layout, session


@ui.page("/login")
def login_page() -> None:
    user = session.current_user()
    if user is not None:
        ui.navigate.to("/")
        return
    with ui.column().classes("w-full items-center justify-center").style("min-height: 100vh"):
        with ui.card().classes("w-96 p-6 gap-3 shadow-lg"):
            ui.label("СПР · Анализ изменений").classes("text-xl font-semibold")
            ui.label("Сравнение двух версий стратегии на одном наборе заявок").classes(
                "text-xs text-slate-500"
            )
            login_input = ui.input("Логин").props("outlined dense").classes("w-full")
            password_input = ui.input("Пароль", password=True, password_toggle_button=True) \
                .props("outlined dense").classes("w-full")
            message = ui.label("").classes("text-red-600 text-sm")

            def submit() -> None:
                user_id = auth.authenticate(login_input.value or "", password_input.value or "")
                if user_id is None:
                    message.set_text("Неверный логин или пароль")
                    return
                session.login(user_id)
                ui.navigate.to("/")

            password_input.on("keydown.enter", lambda _: submit())
            ui.button("Войти", on_click=submit).props("unelevated").classes("w-full")


@ui.page("/")
def runs_page() -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="runs")
    owner = session.user_id(user)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Мои прогоны").classes("text-xl font-semibold")
            ui.space()
            ui.button("Новая конфигурация", icon="add",
                      on_click=lambda: ui.navigate.to("/configs")).props("unelevated")

        items = runs.list_runs(owner)
        if not items:
            layout.empty_state("Создайте конфигурацию и выберите датасет")
        else:
            columns = [
                {"name": "name", "label": "Прогон", "field": "name", "align": "left"},
                {"name": "state", "label": "Состояние", "field": "state", "align": "left"},
                {"name": "pairs", "label": "Заявок", "field": "pairs", "align": "right"},
                {"name": "changed", "label": "С различиями", "field": "changed", "align": "right"},
                {"name": "flips", "label": "Смена решения", "field": "flips", "align": "right"},
                {"name": "created", "label": "Создан", "field": "created", "align": "left"},
                {"name": "open", "label": "", "field": "open", "align": "right"},
            ]
            rows = []
            for item in items:
                summary = item.get("summary") or {}
                rows.append({
                    "id": str(item["id"]),
                    "name": item["name"] or str(item["id"])[:8],
                    "state": layout.STATE_LABEL.get(item["state"], item["state"]),
                    "pairs": layout.fmt_int(item.get("expected_pairs") or 0),
                    "changed": layout.fmt_int(summary.get("changed", 0)),
                    "flips": layout.fmt_int(summary.get("decision_changed", 0)),
                    "created": layout.fmt_time(item.get("created_at")),
                    "open": "открыть",
                })
            table = ui.table(columns=columns, rows=rows, row_key="id",
                             pagination=20).props('rows-per-page-label="Строк на странице:"') \
                .classes("w-full")
            table.on("rowClick", lambda event: ui.navigate.to(
                f"/runs/{event.args['id']}"),
                js_handler="(event, row) => emit({id: row.id})")

        with ui.expansion("Зарегистрированные датасеты и предпроверка", icon="folder_open"):
            known = datasets.list_datasets(owner)
            if not known:
                ui.label("Датасеты не зарегистрированы").classes("text-sm text-slate-500")
            for dataset in known:
                check = datasets.precheck_dataset(dataset["root_ref"], dataset.get("abs_root"),
                                                  dataset.get("file_glob", "*.json"))
                with ui.row().classes("items-center gap-2"):
                    ui.icon("check_circle" if check["ok"] else "error",
                            color="green" if check["ok"] else "red")
                    ui.label(f"{dataset['name']} — {check['pairs']} пар заявок").classes("text-sm")
                    for warning in check["warnings"]:
                        ui.badge(warning, color="orange").classes("text-xs")
                    for error in check["errors"]:
                        ui.badge(error, color="red").classes("text-xs")

        with ui.expansion("Добавить датасет", icon="add_box"):
            found = datasets.discover_dataset_roots()
            options = {d["root_ref"]: d["root_ref"] for d in found}
            if not options:
                ui.label("В DATASET_ROOT не найдено ни одного прогона с manifest.json") \
                    .classes("text-sm text-slate-500")
            else:
                picker = ui.select(options, label="Каталог прогона").props("outlined dense")
                name_input = ui.input("Название").props("outlined dense")
                file_glob = ui.input("Маски файлов (через запятую)", value="*.json").props("outlined dense")

                def add() -> None:
                    if not picker.value:
                        ui.notify("Выберите каталог", type="warning")
                        return
                    try:
                        datasets.register_dataset(owner, name_input.value or picker.value,
                                                  picker.value, file_glob.value)
                    except Exception as exc:  # noqa: BLE001 - show registration validation
                        ui.notify(str(exc), type="negative")
                        return
                    ui.notify("Датасет зарегистрирован", type="positive")
                    ui.navigate.to("/")

                ui.button("Зарегистрировать", on_click=add)
