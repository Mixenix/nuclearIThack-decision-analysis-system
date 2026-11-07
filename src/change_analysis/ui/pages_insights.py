"""Страница «Карта изменений»: агрегация по последствиям, а не по строкам.

Три разреза, которых нет в обычной сводной:

* **архетипы изменений** — у заявок разные *составы* изменений; группируем по составу
  и сразу видим, какие типы последствий дали смену решения, а какие нет;
* **влияние элементов стратегии** — правило, модель, порог, решение по продукту:
  сколько заявок затронуто и сколько из них со сменой решения;
* **что меняется вместе** — пары групп изменений с коэффициентом Жаккара.

Числа берутся из уже посчитанной витрины: страница ничего не пересчитывает и на
детерминированный отчёт не влияет.
"""

from __future__ import annotations

from nicegui import run as background, ui

from ..services import insights
from ..services import runs
from ..services.errors import AccessDenied
from . import layout, session


def _bar(value: float, *, color: str = "primary") -> None:
    """Полоска доли без внешних зависимостей: обычный div с шириной в процентах."""
    percent = max(0.0, min(1.0, float(value))) * 100
    with ui.element("div").classes("w-24 h-2 rounded bg-slate-200"):
        ui.element("div").classes(f"h-2 rounded bg-{color}").style(f"width: {percent:.1f}%")


def _percent(value: float) -> str:
    return f"{float(value) * 100:.0f}%"


@ui.page("/runs/{run_id}/insights")
def insights_page(run_id: str) -> None:
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

    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"Карта изменений · {run['name']}").classes("text-xl font-semibold")
            ui.space()
            ui.button("Прогон", icon="arrow_back",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}")).props("outline dense")
            ui.button("Отчёт проверок", icon="fact_check",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/report")) \
                .props("outline dense")
            ui.button("Сводная", icon="table_chart",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/explore")) \
                .props("outline dense")

        summary_box = ui.column().classes("w-full gap-1")
        archetype_box = ui.column().classes("w-full gap-2")
        element_box = ui.column().classes("w-full gap-2")
        pair_box = ui.column().classes("w-full gap-2")

        def render() -> None:
            summary = insights.summary(run_id)
            archetypes = insights.archetypes(run_id)
            elements = insights.element_impact(run_id)
            pairs = insights.cooccurrence(run_id)

            summary_box.clear()
            with summary_box:
                with ui.card().classes("w-full"):
                    ui.label("Что произошло").classes("font-medium")
                    ui.label(
                        f"Изменения затронули {summary['apps_changed']} заявок из "
                        f"{summary['apps_total']}. Решение изменилось у {summary['flips']}: "
                        f"{summary['logic_flips']} — при неизменных входных данных "
                        f"(похоже на изменение логики), {summary['input_flips']} — вместе "
                        f"с входными данными.").classes("text-sm text-slate-600")
                    with ui.row().classes("gap-3 mt-2 flex-wrap"):
                        for title, count in summary["groups"].items():
                            with ui.element("div").classes(
                                    "px-2 py-1 rounded bg-slate-100 text-xs"):
                                ui.label(f"{title}: {count}")

            archetype_box.clear()
            with archetype_box:
                with ui.card().classes("w-full"):
                    ui.label("Архетипы изменений").classes("font-medium")
                    ui.label("Заявки сгруппированы по составу изменений. Столбец «логика» — "
                             "доля смен решения, у которых внешние скоры не менялись."
                             ).classes("text-xs text-slate-500")
                    if not archetypes:
                        ui.label("Изменений нет").classes("text-sm text-slate-500")
                    for item in archetypes:
                        with ui.row().classes("w-full items-center gap-3 border-b py-1"):
                            ui.label(item["label"]).classes("text-sm w-72 shrink-0")
                            _bar(item["share"], color="primary")
                            ui.label(f"{item['apps']} заявок ({_percent(item['share'])})") \
                                .classes("text-xs text-slate-600 w-40")
                            ui.label(f"смен решения: {item['flips']}") \
                                .classes("text-xs text-slate-600 w-32")
                            ui.label(f"логика: {_percent(item['logic_share'])}") \
                                .classes("text-xs text-slate-600 w-24")
                            ui.link("пример заявки",
                                    f"/runs/{run_id}/app/{item['example_app']}") \
                                .classes("text-xs")

            element_box.clear()
            with element_box:
                with ui.card().classes("w-full"):
                    ui.label("Влияние элементов стратегии").classes("font-medium")
                    ui.label("Правила, модели и пороги вместо путей атрибутов. Влияние = "
                             "затронутые заявки + удвоенные смены решения."
                             ).classes("text-xs text-slate-500")
                    top = max((item["impact"] for item in elements), default=1) or 1
                    for item in elements:
                        with ui.row().classes("w-full items-center gap-3 border-b py-1"):
                            ui.label(item["name"]).classes("text-sm w-72 shrink-0")
                            _bar(item["impact"] / top, color="secondary")
                            ui.label(f"заявок: {item['apps']}").classes(
                                "text-xs text-slate-600 w-28")
                            ui.label(f"смен: {item['flips']}").classes(
                                "text-xs text-slate-600 w-24")
                            ui.label(f"логика: {_percent(item['logic_share'])}").classes(
                                "text-xs text-slate-600 w-24")
                            ui.link("пример", f"/runs/{run_id}/app/{item['example_app']}") \
                                .classes("text-xs")

            pair_box.clear()
            with pair_box:
                with ui.card().classes("w-full"):
                    ui.label("Что меняется вместе").classes("font-medium")
                    ui.label("Коэффициент Жаккара по заявкам. Это корреляция: изменения идут "
                             "вместе, но одно не доказывает другое."
                             ).classes("text-xs text-slate-500")
                    if not pairs:
                        ui.label("Пар не набралось").classes("text-sm text-slate-500")
                    for item in pairs:
                        with ui.row().classes("w-full items-center gap-3 border-b py-1"):
                            ui.label(f"{item['left']} + {item['right']}").classes(
                                "text-sm w-72 shrink-0")
                            _bar(item["jaccard"], color="accent")
                            ui.label(f"вместе: {item['both']}").classes(
                                "text-xs text-slate-600 w-28")
                            ui.label(f"J = {item['jaccard']:.3f}").classes(
                                "text-xs text-slate-600 w-24")
                            ui.label(f"смен: {item['flips']}").classes(
                                "text-xs text-slate-600 w-24")
                            ui.link("пример", f"/runs/{run_id}/app/{item['example_app']}") \
                                .classes("text-xs")

            ui.label("Агрегация по последствиям и элементам стратегии. Детерминированный "
                     "отчёт проверок и сводная не зависят от этой страницы."
                     ).classes("text-xs text-slate-400")

        ui.timer(0.1, render, once=True)
