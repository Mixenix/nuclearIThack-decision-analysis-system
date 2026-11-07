"""Common page shell: header, navigation, and the small formatting helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from nicegui import ui

from ..contracts.types import FilterSpec
from . import session

STATE_LABEL = {
    "QUEUED": "в очереди",
    "RUNNING": "выполняется",
    "SUCCEEDED": "готов",
    "FAILED": "ошибка",
    "INTERRUPTED": "прерван",
    "DONE": "готов",
    "PENDING": "ожидание",
}
STATE_COLOR = {
    "QUEUED": "grey",
    "RUNNING": "blue",
    "SUCCEEDED": "green",
    "FAILED": "red",
    "INTERRUPTED": "orange",
}
RESULT_ICON = {"PASS": "check_circle", "FAIL": "error", "ERROR": "build"}


def fmt_time(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value[:19].replace("T", " ")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def shell(user: dict[str, Any], active: str = "") -> None:
    """Header + navigation.  Called at the top of every authenticated page."""
    with ui.header().classes("items-center gap-4 bg-slate-900 text-white px-4"):
        ui.label("Graeae · Анализ изменений СПР").classes("text-lg font-semibold")
        ui.space()
        for route, label, key in (
            ("/", "Прогоны", "runs"),
            ("/configs", "Конфигурации", "configs"),
            ("/workspace", "Рабочая область", "workspace"),
            ("/checks", "Проверки", "checks"),
        ):
            ui.link(label, route).classes(
                "text-white no-underline px-2 py-1 rounded "
                + ("bg-slate-700" if active == key else "hover:bg-slate-800")
            )
        ui.space()
        ui.label(user.get("display_name") or user["login"]).classes("text-sm opacity-80")

        def do_logout() -> None:
            session.logout()
            ui.navigate.to("/login")

        ui.button("Выйти", on_click=do_logout).props("flat dense color=white")


def error_box(message: str) -> None:
    with ui.card().classes("w-full border-l-4 border-red-500 bg-red-50"):
        ui.label(message).classes("text-red-800 text-sm whitespace-pre-wrap")


def empty_state(message: str) -> None:
    with ui.column().classes("w-full items-center py-12"):
        ui.icon("inbox", size="2rem").classes("text-slate-400")
        ui.label(message).classes("text-slate-500")


def filters_from_ui(
    run_id: str,
    products: Iterable[str] | None = None,
    change_types: Iterable[str] | None = None,
    source: str | None = None,
    decision_changed: bool | None = None,
    path_prefix: str | None = None,
    diff_flag: int | None = None,
) -> FilterSpec:
    def as_tuple(values):
        if not values:
            return None
        return tuple(str(v) for v in values)

    return FilterSpec(
        run_id=run_id,
        products=as_tuple(products),
        change_types=as_tuple(change_types),
        source=source or None,
        decision_changed=decision_changed if decision_changed else None,
        path_prefix=path_prefix or None,
        diff_flag=diff_flag,
    )


def metric_text(row: dict[str, Any]) -> str:
    """Render METRIC without inventing precision, and without hiding its nature."""
    metric = row.get("METRIC", row.get("metric"))
    if metric is None:
        return "—"
    try:
        value = float(metric)
    except (TypeError, ValueError):
        return str(metric)
    if abs(value) <= 1.0:
        return f"{value * 100:.1f}%"
    return f"{value:g}"
