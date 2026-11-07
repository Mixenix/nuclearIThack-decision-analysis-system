"""Check report page, application card, and the checks catalogue."""

from __future__ import annotations

import json
from typing import Any

from nicegui import run as background, ui

from ..checks.loader import list_builtin_checks
from ..llm import adapter as llm
from ..reports.export import is_safe_app_id
from ..services import report as report_service
from ..services import runs
from ..services.errors import AccessDenied
from . import layout, session


@ui.page("/runs/{run_id}/report")
def report_page(run_id: str, revision: int | None = None) -> None:
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
            ui.label(f"Отчёт по прогону · {run['name']}").classes("text-xl font-semibold")
            revision_badge = ui.badge("ожидание публикации", color="slate").props("outline")
            ui.space()
            ui.button("Сводная таблица", icon="table_chart",
                      on_click=lambda: ui.navigate.to(f"/runs/{run_id}/explore")).props("outline")

            download_target = ui.label("").classes("text-xs text-slate-500")

            async def download() -> None:
                download_target.set_text("Формирование HTML…")
                try:
                    path = await background.io_bound(
                        report_service.export_html, owner, run_id, revision,
                        _current_base_url())
                except Exception as exc:  # noqa: BLE001
                    download_target.set_text("Экспорт не удался")
                    ui.notify(f"Экспорт не удался: {exc}", type="negative")
                    return
                download_target.set_text(f"готово: {path.rsplit('/', 1)[-1]}")
                ui.download.file(path, filename=f"report-{run_id[:8]}.html")

            ui.button("Скачать HTML", icon="download", on_click=download).props("unelevated")

        if run["state"] != "SUCCEEDED":
            layout.error_box("Отчёт открывается после завершения обработки. "
                             f"Текущее состояние: {layout.STATE_LABEL.get(run['state'])}.")
            return

        try:
            view = report_service.published_check_report(owner, run_id, revision)
        except Exception as exc:  # noqa: BLE001
            layout.error_box(f"Не удалось построить отчёт: {exc}")
            return

        revision = int(view["revision"])
        revision_badge.set_text(f"ревизия {revision}")
        if int(run["revision"]) > revision:
            latest = report_service.published_check_report(owner, run_id)
            pending = int(latest["revision"]) < int(run["revision"])
            ui.label(f"Показана ревизия {revision}, "
                     + ("идёт публикация ревизии " if pending else "доступна ревизия ")
                     + f"{run['revision']}.").classes("text-sm text-amber-700")
        rows = view["rows"]
        failures = [r for r in rows if r["RESULT"] == "FAIL"]
        technical = view["errors"]

        with ui.row().classes("w-full gap-4"):
            with ui.card().classes("px-4 py-3"):
                ui.label("Строк в отчёте").classes("text-xs text-slate-500")
                ui.label(str(len(rows))).classes("text-2xl font-semibold")
            with ui.card().classes("px-4 py-3"):
                ui.label("FAIL (HIGH)").classes("text-xs text-slate-500")
                ui.label(str(sum(1 for r in failures if r["CONFIDENCE"] == "HIGH"))) \
                    .classes("text-2xl font-semibold text-red-700")
            with ui.card().classes("px-4 py-3"):
                ui.label("FAIL (LOW)").classes("text-xs text-slate-500")
                ui.label(str(sum(1 for r in failures if r["CONFIDENCE"] == "LOW"))) \
                    .classes("text-2xl font-semibold text-amber-600")
            with ui.card().classes("px-4 py-3"):
                ui.label("Технические ошибки").classes("text-xs text-slate-500")
                ui.label(str(len(technical))).classes("text-2xl font-semibold text-orange-700")

        ui.label("Единая таблица проверок").classes("text-lg font-semibold")
        ui.label(view.get("scope", "")).classes("text-xs text-slate-500")

        with ui.column().classes("w-full gap-1"):
            header = ui.row().classes(
                "w-full bg-slate-100 px-2 py-1 text-xs font-semibold text-slate-600 gap-2")
            with header:
                for title, width in (("Проверка", "w-64"), ("Срез", "w-20"),
                                     ("Аргумент", "flex-grow"), ("Числа", "w-28"),
                                     ("Метрика", "w-24"), ("Вердикт", "w-36"),
                                     ("Заявки", "w-24")):
                    ui.label(title).classes(width)
            for item in rows:
                is_low = item["CONFIDENCE"] == "LOW"
                verdicted = item["execution_state"] != "DONE"
                is_fail = item["RESULT"] == "FAIL"
                # Colour is never the only carrier of meaning: the verdict cell
                # also carries an icon and a text label.  HIGH FAIL is loud,
                # LOW FAIL is pale and says "низкая уверенность" in words.
                bg = ("bg-orange-50 border-l-4 border-orange-400" if verdicted else
                      "bg-red-100 border-l-4 border-red-500" if is_fail and not is_low else
                      "bg-amber-50/40 border-l-4 border-amber-200 text-slate-600"
                      if is_fail else "")
                with ui.row().classes(f"w-full px-2 py-1 text-sm items-center gap-2 border-b {bg}"):
                    ui.label(item["LABEL"]).classes("w-64 truncate")
                    ui.label(item["SOURCE"]).classes("w-20 text-xs text-slate-600")
                    ui.label(item["ARGUMENT"] or "—").classes("flex-grow text-xs truncate")
                    ui.label(f"{layout.fmt_int(item['CNT'])} / "
                             f"{layout.fmt_int(item['TOTAL'])}").classes("w-28 text-xs")
                    ui.label(layout.metric_text(item)).classes("w-24 text-xs")
                    with ui.row().classes("w-40 items-center gap-1"):
                        if verdicted:
                            ui.icon("build", color="orange")
                            ui.label("ОШИБКА").classes("text-orange-700 text-xs font-medium")
                        else:
                            icon = layout.RESULT_ICON.get(item["RESULT"], "help")
                            colour = "green" if item["RESULT"] == "PASS" else (
                                "amber" if is_low else "red")
                            ui.icon(icon, color=colour)
                            label = item["RESULT"] + (" · низкая уверенность" if is_low else "")
                            ui.label(label).classes(
                                "text-xs "
                                + ("text-amber-700" if is_low else
                                   "text-red-800 font-semibold" if is_fail else "text-green-700"))
                    with ui.row().classes("w-24 items-center gap-1"):
                        if item["APP_IDS"]:
                            count = len(item["APP_IDS"])
                            ui.button(f"{count}",
                                      on_click=lambda i=item: open_apps(run_id, i)) \
                                .props("flat dense size=sm")
                        else:
                            ui.label("—").classes("text-xs text-slate-400")

        for execution in technical:
            with ui.card().classes("w-full border-l-4 border-orange-500 bg-orange-50"):
                ui.label(f"Техническая ошибка: {execution['check_id']}").classes(
                    "font-medium text-orange-800")
                ui.label((execution.get("error") or "")[:1500]).classes(
                    "text-xs whitespace-pre-wrap text-orange-900")

        # -- LLM second opinion ------------------------------------------
        # The call goes through ``background.io_bound`` (a worker thread): a model
        # request takes seconds, so it must never block the NiceGUI event loop.
        # See PLAN.md §17.3 — the block is optional and never invents an answer.
        ui.label("Независимое мнение ИИ").classes("text-lg font-semibold mt-4")
        opinion_status = llm.status()
        busy: dict[str, bool] = {"value": False}

        async def request_opinion() -> None:
            if busy["value"]:
                return
            busy["value"] = True
            opinion_button.disable()
            opinion_hint.set_text("запрос к модели выполняется… (может занять до "
                                  f"{opinion_status.get('timeout_sec', 60)} с)")
            try:
                result = await background.io_bound(
                    llm.request_second_opinion, run_id, revision, {})
                state = str(result.get("state") or "")
                if state == "DONE":
                    latency = result.get("latency_ms")
                    ui.notify("Мнение ИИ получено"
                              + (f" за {latency} мс" if latency is not None else ""),
                              type="positive")
                elif state == "UNAVAILABLE":
                    ui.notify(f"ИИ недоступен: {result.get('error') or 'нет ключа'}",
                              type="warning")
                else:
                    ui.notify(f"Запрос к ИИ завершился ошибкой: "
                              f"{result.get('error') or 'причина не указана'}", type="negative")
            except Exception as exc:  # noqa: BLE001 - the page must survive
                ui.notify(f"Запрос к ИИ не удался: {exc}", type="negative")
            finally:
                busy["value"] = False
                if not opinion_button.is_deleted:
                    opinion_button.enable()
                    opinion_hint.set_text("")
            if not opinion_box.is_deleted:      # the user may have navigated away
                render_opinion()

        with ui.row().classes("w-full items-center gap-3"):
            opinion_button = ui.button("Независимое мнение ИИ", icon="psychology",
                                       on_click=request_opinion).props("unelevated")
            ui.label("Отправляет модели ограниченный пакет (агрегаты, проверки, "
                     "детерминированную выборку различий) и сохраняет ответ отдельно "
                     "от детерминированных чисел.").classes("text-xs text-slate-500")
            opinion_hint = ui.label("").classes("text-xs text-slate-500")

        opinion_box = ui.column().classes("w-full gap-2")

        def render_opinion() -> None:
            opinion_box.clear()
            try:
                stored = _load_llm_report(owner, run_id, revision)
            except Exception as exc:  # noqa: BLE001
                with opinion_box:
                    layout.error_box(f"Не удалось прочитать мнение ИИ: {exc}")
                return
            with opinion_box:
                with ui.card().classes("w-full"):
                    if not opinion_status.get("available"):
                        ui.label(f"Не настроено: {opinion_status.get('reason', 'нет ключа')}") \
                            .classes("text-sm text-slate-700")
                        ui.label("Блок честно показывает отсутствие интеграции: "
                                 "детерминированный отчёт и проверки от этого не зависят.") \
                            .classes("text-xs text-slate-500")
                        if stored is None:
                            return
                        ui.label("Ниже — ранее сохранённое мнение; текущая интеграция недоступна.") \
                            .classes("text-xs text-slate-500")
                    if stored is None:
                        ui.label(f"Провайдер {opinion_status.get('provider')} / "
                                 f"{opinion_status.get('model')}: запрос ещё не выполнялся.") \
                            .classes("text-sm text-slate-600")
                        ui.label("Нажмите «Независимое мнение ИИ», чтобы получить "
                                 "второе мнение по этому прогону.").classes("text-xs text-slate-500")
                        return

                    state = str(stored.get("state") or "ERROR")
                    colour = {"DONE": "green", "UNAVAILABLE": "amber"}.get(state, "red")
                    with ui.row().classes("items-center gap-3 flex-wrap"):
                        ui.badge(f"состояние: {state}", color=colour).props("outline")
                        ui.label(f"{stored.get('provider')} / {stored.get('model')}") \
                            .classes("text-xs text-slate-600")
                        if stored.get("response_model"):
                            ui.label(f"ответила модель: {stored['response_model']}") \
                                .classes("text-xs text-slate-500")
                        if stored.get("latency_ms") is not None:
                            ui.label(f"задержка: {stored['latency_ms']} мс") \
                                .classes("text-xs text-slate-500")
                        if stored.get("created_at"):
                            ui.label(f"запрошено: {layout.fmt_time(stored['created_at'])}") \
                                .classes("text-xs text-slate-500")
                    if stored.get("payload_hash"):
                        ui.label(f"отпечаток отправленного пакета: {stored['payload_hash']} "
                                 f"({stored.get('payload_chars') or '?'} символов)") \
                            .classes("text-xs text-slate-400")

                    if state != "DONE":
                        ui.label("Мнение ИИ недоступно — это не влияет на числа и вердикты "
                                 "детерминированного отчёта.").classes("text-sm text-amber-800")
                        if stored.get("error"):
                            ui.label(str(stored["error"])).classes(
                                "text-xs text-red-700 whitespace-pre-wrap")
                        return

                    last_attempt = stored.get("last_attempt")
                    if isinstance(last_attempt, dict) and last_attempt:
                        ui.label(
                            f"Последняя попытка ({layout.fmt_time(last_attempt.get('at'))}) "
                            f"завершилась состоянием {last_attempt.get('state')}"
                            + (f": {last_attempt.get('error')}"
                               if last_attempt.get("error") else "")
                            + ". Ниже показан более ранний успешный ответ.") \
                            .classes("text-xs text-amber-700")
                    if stored.get("summary"):
                        ui.label(str(stored["summary"])).classes(
                            "text-sm whitespace-pre-wrap")

                    evidence_by_id = {
                        item.get("evidence_id"): item
                        for item in (stored.get("evidence") or [])
                        if isinstance(item, dict)
                    }
                    observations = [o for o in (stored.get("observations") or [])
                                    if isinstance(o, dict)]
                    if observations:
                        ui.label("Наблюдения модели").classes("font-medium mt-2")
                        for item in observations:
                            with ui.row().classes("w-full items-start gap-2 flex-nowrap"):
                                ui.label("•").classes("text-sm text-slate-500")
                                with ui.column().classes("gap-0 flex-grow"):
                                    ui.label(str(item.get("text") or "")).classes("text-sm")
                                    evidence = evidence_by_id.get(item.get("evidence_id") or "")
                                    if evidence:
                                        with ui.row().classes(
                                                "items-center gap-1 flex-wrap text-xs"):
                                            ui.label(f"evidence {item.get('evidence_id')}:") \
                                                .classes("text-slate-500")
                                            app_id = str(evidence.get("app_id") or "")
                                            if is_safe_app_id(app_id):
                                                ui.link(app_id,
                                                        f"/runs/{run_id}/app/{app_id}") \
                                                    .classes("text-xs")
                                            else:
                                                ui.label(app_id).classes("text-xs")
                                            ui.label(
                                                f"{evidence.get('path')} · "
                                                f"{evidence.get('change_type')} · "
                                                f"до: {evidence.get('before') or '(пусто)'} · "
                                                f"после: {evidence.get('after') or '(пусто)'}"
                                            ).classes("text-xs text-slate-500")
                                    elif item.get("evidence_id"):
                                        ui.label(f"evidence {item.get('evidence_id')}") \
                                            .classes("text-xs text-slate-500")

                    if stored.get("uncertainty"):
                        ui.label("Чего не хватает для вывода").classes("font-medium mt-2")
                        for entry in stored["uncertainty"]:
                            ui.label(f"— {entry}").classes("text-xs text-slate-600")
                    if stored.get("next_steps"):
                        ui.label("Что предлагает проверить").classes("font-medium mt-2")
                        for entry in stored["next_steps"]:
                            ui.label(f"— {entry}").classes("text-xs text-slate-600")
                    if int(stored.get("revision") or revision) != int(revision):
                        ui.label(
                            f"Это мнение относится к ревизии {stored.get('revision')}, "
                            f"текущая ревизия отчёта — {revision}: проверки с тех пор "
                            f"могли измениться. Нажмите кнопку, чтобы получить мнение "
                            f"по текущей."
                        ).classes("text-xs text-amber-700 mt-1")
                    if stored.get("dropped_malformed"):
                        ui.label("Отброшено наблюдений без текста: "
                                 f"{stored['dropped_malformed']}") \
                            .classes("text-xs text-slate-500 mt-1")
                    if stored.get("dropped_evidence"):
                        ui.label(f"Отброшено наблюдений со ссылкой на evidence_id, которого "
                                 f"не было в пакете: {stored['dropped_evidence']}") \
                            .classes("text-xs text-amber-700")
                    ui.label("Это текст модели, а не проверка: он не меняет ни одного числа, "
                             "вердикта или confidence выше. Evidence показано дословно, "
                             "чтобы утверждение можно было сверить с данными.") \
                        .classes("text-xs text-amber-800 bg-amber-50 border border-amber-200 "
                                 "rounded p-2 mt-2")

        render_opinion()

        ui.label("Ограничения").classes("text-sm font-medium mt-2")
        for limitation in _limitations(run):
            ui.label("— " + limitation).classes("text-xs text-slate-600")


def _limitations(run: dict[str, Any]) -> list[str]:
    return [
        "Проверки относятся ко всему прогону; интерфейсные фильтры их не пересчитывают.",
        "APP_IDS в строке проверки — ограниченная выборка, а не полный набор заявок.",
        "Категории источника изменения описывают наблюдаемые признаки, а не доказанную причину.",
        "Детальный разбор требует входа в приложение: ссылки ведут на защищённые страницы.",
    ]


def _load_llm_report(owner: str, run_id: str,
                     revision: int | None = None) -> dict[str, Any] | None:
    """Latest stored opinion for this run/revision (owner-checked, read-only)."""
    try:
        runs.get_run(owner, run_id)          # raises AccessDenied for a foreign run
    except Exception:  # noqa: BLE001
        return None
    return llm.load_report(run_id, revision)


def open_apps(run_id: str, item: dict[str, Any]) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[40rem]"):
        ui.label(f"{item['LABEL']} · {item['ARGUMENT']}").classes("font-medium")
        ui.label(f"Найдено заявок: {layout.fmt_int(item['CNT'])}; "
                 f"в строке проверки показано {len(item['APP_IDS'])} — это примеры, "
                 f"а не полный список.").classes("text-xs text-slate-500")
        for app_id in item["APP_IDS"]:
            ui.link(app_id, f"/runs/{run_id}/app/{app_id}").classes("text-sm")
        ui.button("Закрыть", on_click=dialog.close).props("flat")
    dialog.open()


@ui.page("/runs/{run_id}/app/{app_id}")
def app_page(run_id: str, app_id: str, include_same: bool = False,
             path: str | None = None, array_key: str | None = None,
             change_type: str | None = None) -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="runs")
    owner = session.user_id(user)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-3"):
        try:
            detail = report_service.app_detail(owner, run_id, app_id)
        except Exception as exc:  # noqa: BLE001
            layout.error_box(f"Заявка недоступна: {exc}")
            return
        segments = (detail.get("segments") or [{}])[0]
        with ui.row().classes("items-center gap-3"):
            ui.label(f"Заявка {app_id}").classes("text-xl font-semibold")
            ui.badge(segments.get("decision_before", ""), color="slate")
            ui.icon("arrow_forward")
            ui.badge(segments.get("decision_after", ""),
                     color="green" if segments.get("decision_after") == "APPROVE" else "red")
            ui.space()
            ui.button("Назад", on_click=lambda: ui.navigate.back()).props("flat")

        with ui.row().classes("gap-4 w-full items-start"):
            with ui.card().classes("flex-grow"):
                ui.label("Слой разрезов").classes("font-medium")
                for field, value in (
                    ("Продукты", segments.get("products")),
                    ("Топ-оффер BEFORE", segments.get("top_offer_before")),
                    ("Топ-оффер AFTER", segments.get("top_offer_after")),
                    ("Целевая модель BEFORE", segments.get("pd_model_before")),
                    ("Целевая модель AFTER", segments.get("pd_model_after")),
                    ("Порог BEFORE", segments.get("cutoff_before")),
                    ("Порог AFTER", segments.get("cutoff_after")),
                    ("Причина отказа BEFORE", segments.get("reject_reason_before")),
                    ("Причина отказа AFTER", segments.get("reject_reason_after")),
                    ("score_okb BEFORE / AFTER",
                     f"{segments.get('score_okb_before')} → {segments.get('score_okb_after')}"),
                    ("Источник изменения", segments.get("cause_category")),
                ):
                    with ui.row().classes("w-full justify-between text-sm"):
                        ui.label(field).classes("text-slate-500")
                        ui.label(str(value or "—")).classes("font-mono text-xs")
            with ui.card().classes("flex-grow"):
                ui.label("Продукты по срезам").classes("font-medium")
                for product in detail.get("products") or []:
                    with ui.row().classes("text-xs font-mono gap-2"):
                        ui.label(product["source"]).classes("w-14 text-slate-500")
                        ui.label(str(product["product_id"])).classes("w-16")
                        ui.label(product["decision"] or "—").classes("w-24")
                        ui.label(product["pd_model"] or "—").classes("w-40")
                        ui.label(product["cutoff"] or "—").classes("w-40")

        ui.label("Различия атрибутов").classes("text-lg font-semibold")
        context = {key: value for key, value in
                   (("path", path), ("array_key", array_key), ("change_type", change_type))
                   if value is not None}
        if context:
            with ui.row().classes("items-center bg-amber-50 border rounded p-2"):
                labels = {"path": "PATH", "array_key": "ARRAY_KEY", "change_type": "тип"}
                ui.label("контекст: " + ", ".join(
                    f"{labels[key]}={value}" for key, value in context.items()))
                ui.button("сбросить", on_click=lambda: ui.navigate.to(
                    f"/runs/{run_id}/app/{app_id}")).props("flat dense")
        switch = ui.switch("показывать совпадающие атрибуты",
                           value=include_same or change_type == "SAME")
        attributes = ui.column().classes("w-full gap-0")

        def render_attributes() -> None:
            attributes.clear()
            try:
                rows = report_service.attribute_rows(owner, run_id, app_id,
                                                     include_same=bool(switch.value))
                if context:
                    rows = [row for row in rows if all(
                        str(row.get(key) or "") == value for key, value in context.items())]
            except Exception as exc:  # noqa: BLE001
                with attributes:
                    layout.error_box(str(exc))
                return
            with attributes:
                if context and not rows:
                    ui.label("В этом контексте атрибуты не найдены.").classes("text-sm")
                with ui.row().classes("w-full bg-slate-100 px-2 py-1 text-xs font-semibold gap-2"):
                    for title, width in (("PATH", "flex-grow"), ("ARRAY_KEY", "w-48"),
                                         ("BEFORE", "w-40"), ("AFTER", "w-40"),
                                         ("Тип", "w-32")):
                        ui.label(title).classes(width)
                for row in rows:
                    colour = "text-red-700" if row["change_type"] != "SAME" else "text-slate-500"
                    with ui.row().classes(f"w-full px-2 py-0.5 text-xs gap-2 border-b {colour}"):
                        ui.label(row["path"]).classes("flex-grow font-mono truncate")
                        ui.label(row["array_key"] or "—").classes("w-48 font-mono truncate")
                        ui.label(str(row["value_before"]) if row["value_before"] is not None
                                 else "—").classes("w-40 font-mono truncate")
                        ui.label(str(row["value_after"]) if row["value_after"] is not None
                                 else "—").classes("w-40 font-mono truncate")
                        ui.label(row["change_type"]).classes("w-32")

        switch.on_value_change(lambda _: render_attributes())
        render_attributes()

        with ui.expansion("Исходный JSON", icon="data_object"):
            with ui.row().classes("gap-2"):
                source_toggle = ui.toggle(["BEFORE", "AFTER"], value="BEFORE").props("dense")
            raw_box = ui.code("").classes("w-full").style("max-height: 30rem; overflow: auto")

            def render_raw() -> None:
                try:
                    payload = report_service.raw_json(owner, run_id, app_id,
                                                      str(source_toggle.value))
                except Exception as exc:  # noqa: BLE001
                    raw_box.set_content(f"недоступно: {exc}")
                    return
                raw_box.set_content(json.dumps(payload, ensure_ascii=False, indent=2))

            source_toggle.on_value_change(lambda _: render_raw())
            render_raw()


@ui.page("/checks")
def checks_catalogue_page() -> None:
    user = session.require_user()
    if user is None:
        return
    layout.shell(user, active="checks")
    owner = session.user_id(user)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        ui.label("Проверки").classes("text-xl font-semibold")
        ui.label("Контракт: файл экспортирует CHECK с id, label, params, entrypoint; "
                 "run(mart, params) возвращает строки PASS/FAIL. "
                 "Выданные файлы не изменяются.").classes("text-xs text-slate-500")
        row = ui.row().classes("gap-4")
        with row:
            for spec in list_builtin_checks():
                with ui.card().classes("w-80"):
                    ui.label(spec.label).classes("font-medium")
                    ui.label(f"id: {spec.id}").classes("text-xs text-slate-500")
                    ui.code(json.dumps(spec.params, ensure_ascii=False, indent=2)) \
                        .classes("w-full").style("max-height: 10rem; overflow:auto")
                    ui.label(f"файл: {spec.source_ref.split('/')[-1]}").classes(
                        "text-xs text-slate-400")
                    ui.button("Открыть в рабочей области",
                              on_click=lambda s=spec: ui.navigate.to(
                                  f"/workspace?file={s.source_ref.split('/')[-1]}"
                                  f"&stage=check")).props("flat dense")
        ui.separator()
        ui.label("Свои проверки").classes("text-lg font-semibold")
        from ..services import procedures

        own = procedures.list_procedures(owner, "check")
        if not own:
            ui.label("Нет собственных проверок — создайте в рабочей области").classes(
                "text-sm text-slate-500")
        for item in own:
            ui.link(item["name"], f"/workspace?procedure_id={item['id']}").classes("text-sm")


def _current_base_url() -> str:
    """Address the page is being viewed at (scheme + host), best effort.

    Behind a tunnel the host is the tunnel's name, on a LAN it is the machine's
    address; either way the exported report links back to the same place the
    reader opened.  Falls back to the configured value if the request is not
    available.
    """
    try:
        from nicegui import context

        request = getattr(context.client, "request", None)
        if request is None:
            return ""
        headers = getattr(request, "headers", {}) or {}
        host = headers.get("host") or getattr(request.url, "netloc", "")
        if not host:
            return ""
        scheme = headers.get("x-forwarded-proto") or getattr(request.url, "scheme", "http")
        return f"{scheme}://{host}"
    except Exception:  # noqa: BLE001 - never break the download
        return ""
