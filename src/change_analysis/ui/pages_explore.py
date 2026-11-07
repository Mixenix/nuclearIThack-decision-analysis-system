"""The interactive pivot and its drill-down.

The reported measure is *distinct applications*, never the sum of occurrences:
one application contributes hundreds of attribute rows and appears in both
slices, so ``SUM(cnt)`` would count the same person many times (PLAN.md §16.2).
The layout (which dimensions are rows, which are columns) is chosen by the user
at query time and recomputed server-side.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote, urlencode
from typing import Any

from nicegui import run as background, ui

from ..contracts.types import PIVOT_DIMENSIONS, FilterSpec
from ..services import report as report_service
from ..services import runs
from ..services.errors import AccessDenied
from . import layout, session

DIM_LABEL = {
    "PREFIX": "PREFIX",
    "CLASS": "CLASS",
    "SUFFIX": "SUFFIX",
    "ARRAY_KEY": "ARRAY_KEY (ключ массива)",
    "KEY": "KEY (атрибут)",
    "PATH": "PATH",
    "VALUE_ADJUSTED": "Значение / бакет",
    "DIFF_FLAG": "Признак различия",
    "CHANGE_TYPE": "Тип изменения",
    "SOURCE": "Срез",
}


def _combo_key(values: list[str]) -> str:
    return "|".join(quote(str(v), safe="") for v in values)


@ui.page("/runs/{run_id}/explore")
def explore_page(run_id: str, decision_changed: bool | None = None,
                 diff_flag: int | None = None) -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="runs")
    owner = session.user_id(user)
    try:
        run = runs.get_run(owner, run_id)
    except AccessDenied:
        with ui.column().classes("w-full max-w-4xl mx-auto p-4"):
            layout.error_box("Этот прогон принадлежит другому пользователю.")
        return
    except Exception as exc:  # noqa: BLE001
        with ui.column().classes("w-full max-w-4xl mx-auto p-4"):
            layout.error_box(f"Прогон недоступен: {exc}")
        return
    if run["state"] != "SUCCEEDED":
        with ui.column().classes("w-full max-w-4xl mx-auto p-4"):
            layout.error_box("Сводная таблица доступна после завершения обработки.")
        return

    state: dict[str, Any] = {"rows": ["PATH"], "cols": ["CHANGE_TYPE"], "cell": None}

    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-3"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"Сводная таблица · {run['name']}").classes("text-xl font-semibold")
            ui.space()
            ui.button("Отчёт проверок", icon="assignment",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/report")).props("outline")

        # -- layout controls ------------------------------------------------
        with ui.card().classes("w-full"):
            ui.label("Раскладка").classes("font-medium")
            ui.label("Измерения выбирает пользователь; агрегация пересчитывается "
                     "на сервере, мера — уникальные заявки.").classes(
                "text-xs text-slate-500")
            with ui.row().classes("w-full gap-4 items-start"):
                rows_select = ui.select(list(PIVOT_DIMENSIONS), label="Строки",
                                       multiple=True, value=["PATH"]) \
                    .props("outlined dense use-chips").classes("w-64")
                cols_select = ui.select(list(PIVOT_DIMENSIONS), label="Колонки",
                                       multiple=True, value=["CHANGE_TYPE"]) \
                    .props("outlined dense use-chips").classes("w-64")
                with ui.column().classes("gap-1"):
                    ui.label("Фильтры").classes("text-xs text-slate-500")
                    try:
                        products = report_service.available_products(owner, run_id)
                    except Exception:  # noqa: BLE001
                        products = []
                    product_select = ui.select(products, label="Продукт", multiple=True) \
                        .props("outlined dense use-chips").classes("w-52")
                    type_select = ui.select(["SAME", "VALUE_CHANGED", "ATTRIBUTE_NEW",
                                             "ATTRIBUTE_LOST"],
                                            label="Тип изменения", multiple=True) \
                        .props("outlined dense use-chips").classes("w-52")
                    source_select = ui.select(["BEFORE", "AFTER"], label="Срез") \
                        .props("outlined dense clearable").classes("w-52")
                    path_input = ui.input("PATH начинается с").props("outlined dense") \
                        .classes("w-52")
                    decision_select = ui.select(
                        {"all": "все", "changed": "только со сменой решения",
                         "unchanged": "только без смены решения"},
                        label="Решение", value=("all" if decision_changed is None else
                                                "changed" if decision_changed else "unchanged"),
                    ).props("outlined dense").classes("w-64")
                    diff_select = ui.select(
                        {"all": "все", "1": "только различия", "0": "только совпадения"},
                        label="Атрибуты", value=str(diff_flag) if diff_flag in (0, 1) else "all",
                    ).props("outlined dense").classes("w-64")
            with ui.row().classes("gap-2 mt-2"):
                ui.button("Пересчитать", icon="refresh", on_click=lambda: recompute()) \
                    .props("unelevated dense")
                total_label = ui.label("").classes("text-sm text-slate-600")

        matrix_container = ui.column().classes("w-full gap-0")
        drill_container = ui.column().classes("w-full gap-2")

    def current_filters() -> FilterSpec:
        return FilterSpec(
            run_id=run_id,
            products=tuple(product_select.value) if product_select.value else None,
            change_types=tuple(type_select.value) if type_select.value else None,
            source=source_select.value or None,
            path_prefix=path_input.value or None,
            decision_changed={"all": None, "changed": True,
                              "unchanged": False}[decision_select.value],
            diff_flag=None if diff_select.value == "all" else int(diff_select.value),
        )

    recompute_lock = asyncio.Lock()

    async def recompute() -> None:
        async with recompute_lock:
            await render_matrix()

    async def render_matrix() -> None:
        dims_rows = list(rows_select.value or [])
        dims_cols = list(cols_select.value or [])
        filters = current_filters()
        state["rows"], state["cols"], state["cell"] = dims_rows, dims_cols, None
        state["filters"] = filters
        drill_container.clear()
        matrix_container.clear()
        try:
            result = await background.io_bound(
                report_service.pivot, owner, run_id, dims_rows, dims_cols, filters)
        except Exception as exc:  # noqa: BLE001
            with matrix_container:
                layout.error_box(f"Не удалось построить сводную: {exc}")
            return
        total = result.get("grand_total") or 0
        total_label.set_text(f"Уникальных заявок в выборке: {layout.fmt_int(total)}")
        row_values = result["row_dim_values"]
        col_values = result["col_dim_values"]
        cells = result["cells"]
        if not dims_rows and not dims_cols and total:
            row_values, col_values = [[]], [[]]
            cells = {"|": {"cnt": total}}
        if total == 0:
            with matrix_container:
                layout.empty_state("В выборке нет заявок — измените фильтры")
            return

        # keep the table readable: top rows by their own total
        def row_total(values: list[str]) -> int:
            key = _combo_key(values) + "|"
            return sum(cell["cnt"] for cell_key, cell in cells.items()
                       if cell_key.startswith(key))

        if len(row_values) * max(1, len(col_values)) > 600:
            row_values = sorted(row_values, key=row_total, reverse=True)[:25]

        with matrix_container:
            with ui.row().classes(
                    "w-full bg-slate-100 px-2 py-1 text-xs font-semibold gap-2"):
                ui.label(" / ".join(DIM_LABEL.get(d, d) for d in dims_rows) or "Все заявки") \
                    .classes("flex-grow")
                for combo in col_values:
                    ui.label(" / ".join(combo) or "—").classes(
                        "w-24 text-right truncate").tooltip(" / ".join(combo) or "—")
                ui.label("Итого").classes("w-20 text-right shrink-0")
            for combo in row_values:
                row_key = _combo_key(combo)
                with ui.row().classes("w-full px-2 py-0 border-b items-center gap-2"):
                    ui.label(" / ".join(combo) or "—").classes(
                        "flex-grow text-xs truncate").tooltip(" / ".join(combo) or "—")
                    for col_combo in col_values:
                        cell = cells.get(row_key + "|" + _combo_key(col_combo))
                        count = cell["cnt"] if cell else 0
                        if count:
                            ui.button(layout.fmt_int(count),
                                      on_click=lambda r=combo, c=col_combo:
                                      show_cell(r, c)).props("flat dense size=sm") \
                                .classes("w-24 justify-end")
                        else:
                            ui.label("·").classes("w-24 text-right text-slate-300")
                    ui.label(layout.fmt_int(row_total(combo))).classes(
                        "w-20 text-right text-xs text-slate-600")
            with ui.row().classes("w-full px-2 py-1 font-semibold text-xs bg-slate-50 gap-2"):
                ui.label("Итого").classes("flex-grow")
                for col_combo in col_values:
                    key = "|" + _combo_key(col_combo)
                    subtotal = sum(cell["cnt"] for cell_key, cell in cells.items()
                                   if cell_key.endswith(key))
                    ui.label(layout.fmt_int(subtotal)).classes("w-24 text-right")
                ui.label(layout.fmt_int(total)).classes("w-20 text-right")
            ui.label("Итог по строкам и столбцам — сумма ячеек; он может повторно "
                     "учитывать заявку, поэтому общий итог считается по уникальным "
                     "APP_ID и не обязан совпадать с суммой.").classes(
                "text-xs text-slate-500 mt-1")

    async def show_cell(row_combo: list[str], col_combo: list[str]) -> None:
        drill_container.clear()
        dims_rows, dims_cols = state["rows"], state["cols"]
        cell_key = _combo_key(row_combo) + "|" + _combo_key(col_combo)
        context = urlencode({
            dim.lower(): str(value) for dim, value in
            zip(dims_rows + dims_cols, row_combo + col_combo)
            if dim in {"PATH", "ARRAY_KEY", "CHANGE_TYPE"}
        })
        with drill_container:
            ui.label(f"Ячейка: {' / '.join(row_combo) or '—'} × "
                     f"{' / '.join(col_combo) or '—'}").classes("font-medium mt-3")
            page = {"value": 1}
            body = ui.column().classes("w-full gap-1")

            async def load() -> None:
                body.clear()
                try:
                    result = await background.io_bound(
                        report_service.drill, owner, run_id, dims_rows, dims_cols, state["filters"],
                        cell_key, page=page["value"], page_size=50)
                except Exception as exc:  # noqa: BLE001
                    with body:
                        layout.error_box(f"Не удалось получить заявки: {exc}")
                    return
                with body:
                    ui.label(f"Уникальных заявок: {layout.fmt_int(result['total'])} "
                             f"(показано {len(result['app_ids'])})").classes(
                        "text-sm text-slate-600")
                    for app_id in result["app_ids"]:
                        ui.link(app_id, f"/runs/{run_id}/app/{quote(app_id, safe='')}"
                                + (f"?{context}" if context else "")).classes("text-sm")
                    pages = max(1, -(-int(result["total"]) // 50))
                    with ui.row().classes("gap-2 items-center"):
                        async def previous() -> None:
                            page["value"] = max(1, page["value"] - 1)
                            await load()

                        async def next_page() -> None:
                            page["value"] = min(pages, page["value"] + 1)
                            await load()

                        ui.button("◀", on_click=previous).props("flat dense")
                        ui.label(f"стр. {page['value']} / {pages}").classes("text-xs")
                        ui.button("▶", on_click=next_page).props("flat dense")

            await load()

    rows_select.on_value_change(lambda _: recompute())
    cols_select.on_value_change(lambda _: recompute())
    ui.timer(0.01, recompute, once=True)
