"""Live run page: stage log, progress, jump to the code that executed."""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ..services import report as report_service
from ..services import runs
from ..services.errors import AccessDenied
from . import layout, session


@ui.page("/runs/{run_id}")
def run_page(run_id: str) -> None:
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

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(run["name"] or "Прогон").classes("text-xl font-semibold")
            state_badge = ui.badge("", color="grey")
            ui.space()
            report_button = ui.button("Отчёт", icon="assignment",
                                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/report"))
            explore_button = ui.button("Сводная таблица", icon="table_chart",
                                       on_click=lambda: ui.navigate.to(f"/runs/{run_id}/explore"))
            ui.button("Карта изменений", icon="insights",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/insights")) \
                .props("outline")
            retry_button = ui.button("Повторить", icon="refresh",
                                     on_click=lambda: do_retry()).props("outline")
        ui.label(f"Датасет: {run.get('dataset_name') or '—'} · "
                 f"создан {layout.fmt_time(run.get('created_at'))} · "
                 f"backend: {run['backend']}").classes("text-xs text-slate-500")

        summary_row = ui.row().classes("w-full gap-6")
        progress_bar = ui.linear_progress(value=0.0, show_value=False).classes("w-full")
        progress_label = ui.label("").classes("text-sm text-slate-600")

        ui.label("Этапы выполнения").classes("text-lg font-semibold mt-2")
        steps_container = ui.column().classes("w-full gap-2")

        ui.label("Журнал").classes("text-lg font-semibold mt-2")
        log_container = ui.column().classes(
            "w-full gap-0 font-mono text-xs bg-slate-50 border rounded p-2"
        ).style("max-height: 340px; overflow-y: auto")

        error_container = ui.column().classes("w-full")

    state: dict[str, Any] = {"event_id": 0, "rendered_steps": "", "finished": False}

    def do_retry() -> None:
        try:
            runs.retry(owner, run_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(str(exc), type="negative")
            return
        state["finished"] = False
        ui.notify("Повторный запуск поставлен в очередь", type="positive")

    def step_row(step: dict[str, Any]) -> None:
        with ui.row().classes("w-full items-center gap-3 border rounded px-3 py-2"):
            icon = {"DONE": "check_circle", "RUNNING": "autorenew", "FAILED": "error",
                    "PENDING": "schedule"}.get(step["state"], "help")
            color = {"DONE": "green", "RUNNING": "blue", "FAILED": "red"}.get(
                step["state"], "grey")
            ui.icon(icon, color=color)
            ui.label({"normalize": "Нормализация", "compare": "Сравнение",
                      "aggregate": "Агрегация"}.get(step["stage"], step["stage"])) \
                .classes("w-28 font-medium")
            total = step.get("progress_total") or 0
            done = step.get("progress_done") or 0
            text = f"{layout.fmt_int(done)}"
            if total:
                text += f" / {layout.fmt_int(total)}"
            ui.label(text).classes("w-40 text-sm text-slate-600")
            if step.get("started_at") and step.get("finished_at"):
                seconds = (step["finished_at"] - step["started_at"]).total_seconds()
                ui.label(f"{seconds:.1f} c").classes("w-20 text-xs text-slate-500")
            else:
                ui.label("").classes("w-20")
            ui.badge(layout.STATE_LABEL.get(step["state"], step["state"]),
                     color=color).props("outline")
            ui.space()
            if step.get("code_changed_since_run"):
                ui.badge("код изменён после запуска", color="orange").props("outline")
            ui.label(f"{step.get('procedure_kind')}: "
                     f"{str(step.get('procedure_ref') or '').split('/')[-1]}").classes(
                "text-xs text-slate-500")
            ui.button("к коду", icon="code",
                      on_click=lambda s=step: open_code(s)).props("flat dense")
            if step.get("error"):
                ui.label(step["error"].splitlines()[0][:160]).classes("text-xs text-red-600")

    def open_code(step: dict[str, Any]) -> None:
        ref = str(step.get("procedure_ref") or "")
        stage = step["stage"]
        if step.get("procedure_kind") == "workspace" and ref:
            ui.navigate.to(f"/workspace?procedure_id={ref}")
        else:
            ui.navigate.to(f"/workspace?stage={stage}&file={ref.split('/')[-1]}")

    def render_summary(summary: dict[str, Any]) -> None:
        summary_row.clear()
        cards = [
            ("Пар заявок", summary.get("pairs")),
            ("С различиями", summary.get("changed")),
            ("Смена решения", summary.get("decision_changed")),
            ("Строк витрины", summary.get("rows")),
            ("Фактов сводной", summary.get("facts")),
        ]
        with summary_row:
            for title, value in cards:
                with ui.card().classes("px-4 py-3"):
                    ui.label(title).classes("text-xs text-slate-500")
                    ui.label(layout.fmt_int(value or 0)).classes("text-2xl font-semibold")
            if summary.get("changed") is not None and summary.get("decision_changed") is not None:
                count = int(summary["changed"]) - int(summary["decision_changed"])
                ui.link(
                    f"Различия есть, решение не изменилось: {layout.fmt_int(count)}",
                    f"/runs/{run_id}/explore?decision_changed=false&diff_flag=1",
                ).classes("text-sm self-center")

    def refresh() -> None:
        try:
            status = runs.status_summary(owner, run_id)
        except Exception as exc:  # noqa: BLE001
            progress_label.set_text(f"не удалось обновить состояние: {exc}")
            return
        state_name = status["state"]
        state_badge.set_text(layout.STATE_LABEL.get(state_name, state_name))
        state_badge.props(f"color={layout.STATE_COLOR.get(state_name, 'grey')}")
        expected = status["expected_pairs"] or 0
        # Worker progress uses stage-specific units, including comparison rows.
        # Do not label that counter as application pairs or an overall percent.
        done = expected if state_name == "SUCCEEDED" else 0
        progress_bar.props("indeterminate" if state_name == "RUNNING" else "",
                           remove="indeterminate" if state_name != "RUNNING" else "")
        progress_bar.set_value(1.0 if state_name == "SUCCEEDED" else 0.0)
        progress_label.set_text(
            f"Обработано {layout.fmt_int(done)} из {layout.fmt_int(expected)} пар"
            if state_name == "SUCCEEDED" else
            f"В выборке {layout.fmt_int(expected)} пар; прогресс указан по этапам"
            if expected else "Ожидание запуска"
        )
        steps = runs.run_stages(owner, run_id)
        steps_key = repr(steps)
        if steps_key != state["rendered_steps"]:
            state["rendered_steps"] = steps_key
            steps_container.clear()
            with steps_container:
                if not steps:
                    ui.label("Этапы ещё не начались").classes("text-sm text-slate-500")
                for step in steps:
                    step_row(step)

        events = runs.run_events(owner, run_id, after_id=state["event_id"], limit=200)
        if events:
            with log_container:
                for event in events:
                    colour = {"ERROR": "text-red-600", "WARNING": "text-orange-600"}.get(
                        event["level"], "text-slate-700")
                    ui.label(f"{layout.fmt_time(event['created_at'])} "
                             f"{event['level']:<7} {event['message']}").classes(colour)
            state["event_id"] = events[-1]["id"]

        finished = status["finished"]
        report_button.set_visibility(state_name == "SUCCEEDED")
        explore_button.set_visibility(state_name == "SUCCEEDED")
        retry_button.set_visibility(state_name in ("FAILED", "INTERRUPTED"))
        if finished and not state["finished"]:
            state["finished"] = True
            render_summary(status.get("summary") or {})
            error_container.clear()
            if status.get("error_message"):
                with error_container:
                    layout.error_box(status["error_message"])

    refresh()
    ui.timer(1.0, refresh)
