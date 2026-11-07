"""Regression coverage for filters, publication and pivot cell context."""

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from nicegui import Client, ui

from change_analysis.contracts.types import FilterSpec
from change_analysis.services import report
from change_analysis.services.errors import InvalidRequest
from change_analysis.ui import pages_explore, pages_report, pages_run, pages_workspace


def check_view(revision=3):
    return dict(revision=revision, rows=[], executions=[], errors=[], scope="Весь прогон",
                counts=dict(high_fail=7, low_fail=2, errors=0))


def test_false_filter_preserves_zero_and_data_revision(monkeypatch):
    monkeypatch.setattr(report, "_owned", lambda *a: dict(revision=3, data_revision=1))
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        return [{"n": 117}]

    monkeypatch.setattr(report, "query", query)
    result = report.pivot("u", "r", [], [],
                          FilterSpec(run_id="r", decision_changed=False, diff_flag=1))
    assert result["grand_total"] == 117
    assert "s.decision_changed = %s" in calls[0][0]
    assert calls[0][1] == ["r", 1, 0, 1]


def test_export_rejects_unpublished_revision(monkeypatch):
    from change_analysis.services import checks

    monkeypatch.setattr(report, "_owned", lambda *a: dict(revision=4, data_revision=1))
    monkeypatch.setattr(report, "query", lambda *a: [{"revision": 3}, {"revision": 1}])
    monkeypatch.setattr(checks, "check_report", lambda u, r, rev: check_view(rev))
    assert report.published_check_report("u", "r")["revision"] == 3
    with pytest.raises(InvalidRequest, match="не опубликована"):
        report.export_html("u", "r", 4)


def test_export_uses_published_filename(monkeypatch, tmp_path):
    from change_analysis import config
    from change_analysis.reports import export

    monkeypatch.setattr(report, "_owned", lambda *a: dict(id="r", revision=4, data_revision=1))
    monkeypatch.setattr(report, "published_check_report", lambda *a: check_view(3))
    monkeypatch.setattr(config, "load_settings", lambda: SimpleNamespace(
        artifact_root=tmp_path, public_base_url="http://localhost"))
    monkeypatch.setattr(export, "export_report", lambda r, path, base: path)
    assert report.export_html("u", "r").endswith("/r/report-rev3.html")


def test_summary_check_counters_and_conclusion_use_displayed_revision():
    summary = dict(cards=[dict(id="checks", high_fail=99)], total_pairs=2002,
                   changed_apps=502, decision_flips=385, conclusion="old")
    report.align_summary_checks(summary, check_view())
    assert summary["cards"][0]["high_fail"] == 7
    assert summary["cards"][0]["low_fail"] == 2
    assert summary["conclusion"].endswith(": 7.")


@pytest.fixture
def page_client(monkeypatch):
    original_on_click = ui.button.on_click

    def on_click(button, callback):
        button._demo_callback = callback
        return original_on_click(button, callback)

    monkeypatch.setattr(ui.button, "on_click", on_click)
    monkeypatch.setattr(pages_run.session, "require_user", lambda: {"id": "u"})
    monkeypatch.setattr(pages_run.layout, "shell", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "timer", lambda *a, **kw: SimpleNamespace(deactivate=lambda: None))
    client = Client(ui.page("/demo-regression"))
    with client:
        yield client
    client.delete()


def texts(client):
    return [getattr(e, "text", "") for e in client.elements.values()]


def click(client, text):
    element = next(e for e in client.elements.values() if getattr(e, "text", "") == text)
    return element._demo_callback()


def test_run_page_renders_ready_selection(page_client, monkeypatch):
    run = dict(name="demo", backend="cpu", state="SUCCEEDED")
    monkeypatch.setattr(pages_run.runs, "get_run", lambda *a: run)
    monkeypatch.setattr(pages_run.runs, "status_summary", lambda *a: dict(
        state="SUCCEEDED", expected_pairs=2002, finished=True,
        summary=dict(changed=502, decision_changed=385)))
    monkeypatch.setattr(pages_run.runs, "run_stages", lambda *a: [])
    monkeypatch.setattr(pages_run.runs, "run_events", lambda *a, **kw: [])
    pages_run.run_page("r")
    link = next(e for e in page_client.elements.values()
                if getattr(e, "text", "").endswith("не изменилось: 117"))
    assert link._props["href"].endswith("?decision_changed=false&diff_flag=1")


@pytest.mark.parametrize("latest,notice", [(3, "идёт публикация ревизии 4."),
                                         (4, "доступна ревизия 4.")])
def test_report_page_labels_displayed_revision(page_client, monkeypatch, latest, notice):
    monkeypatch.setattr(pages_report.runs, "get_run", lambda *a: dict(
        name="demo", state="SUCCEEDED", revision=4))
    monkeypatch.setattr(report, "published_check_report", lambda *a:
                        check_view(3 if len(a) == 3 else latest))
    monkeypatch.setattr(pages_report.llm, "status", lambda: {"available": False})
    monkeypatch.setattr(pages_report, "_load_llm_report", lambda *a: None)
    pages_report.report_page("r", revision=3)
    assert "ревизия 3" in texts(page_client)
    assert f"Показана ревизия 3, {notice}" in texts(page_client)


@pytest.mark.parametrize("context,expected", [({}, ["keep", "other"]),
    ({"path": "keep", "array_key": "a&б", "change_type": "SAME"}, ["keep"])])
def test_attribute_context_filters_existing_service(page_client, monkeypatch, context, expected):
    monkeypatch.setattr(report, "app_detail", lambda *a: {})
    calls = []

    def attributes(*a, **kw):
        calls.append(kw)
        return [dict(path=p, array_key="a&б", change_type="SAME",
                     value_before="v", value_after="v") for p in ("keep", "other")]

    monkeypatch.setattr(report, "attribute_rows", attributes)
    monkeypatch.setattr(report, "raw_json", lambda *a: {})
    pages_report.app_page("r", "app", **context)
    assert [t for t in texts(page_client) if t in {"keep", "other"}] == expected
    if context:
        assert calls == [{"include_same": True}]
        assert any(t.startswith("контекст: PATH=keep") for t in texts(page_client))
        assert "сбросить" in texts(page_client)


def test_pivot_link_encodes_selected_dimensions(page_client, monkeypatch):
    timers = []
    monkeypatch.setattr(ui, "timer", lambda interval, callback, **kw: timers.append(callback))
    monkeypatch.setattr(pages_explore.runs, "get_run", lambda *a: dict(state="SUCCEEDED", name="demo"))
    monkeypatch.setattr(report, "available_products", lambda *a: [])
    path = "a.б / x&y"
    captured = []

    def pivot(*args):
        captured.append(args[-1])
        key = pages_explore._combo_key([path]) + "|SAME"
        return dict(grand_total=1, row_dim_values=[[path]], col_dim_values=[["SAME"]],
                    cells={key: {"cnt": 1}})

    async def io_bound(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(pages_explore.background, "io_bound", io_bound)
    monkeypatch.setattr(report, "pivot", pivot)
    monkeypatch.setattr(report, "drill", lambda *a, **kw: dict(total=1, app_ids=["app"]))
    pages_explore.explore_page("r", decision_changed=False, diff_flag=1)
    asyncio.run(timers[0]())
    asyncio.run(click(page_client, "1"))
    link = next(e for e in page_client.elements.values() if getattr(e, "text", "") == "app")
    href = link._props["href"]
    assert path not in href
    assert parse_qs(urlsplit(href).query) == {"path": [path], "change_type": ["SAME"]}
    assert captured[0].decision_changed is False
    assert captured[0].diff_flag == 1


def test_workspace_waits_for_publication(page_client, monkeypatch):
    timers = []
    monkeypatch.setattr(ui, "timer", lambda interval, callback, **kw:
                        timers.append((interval, callback)) or SimpleNamespace(deactivate=lambda: None))
    monkeypatch.setattr(ui, "run_javascript", lambda *a: None)
    monkeypatch.setattr(ui, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(pages_workspace.procedures, "list_procedures", lambda *a: [])
    monkeypatch.setattr(pages_workspace.procedures, "list_registered_files", lambda: [])
    monkeypatch.setattr(pages_workspace.procedures, "save_procedure", lambda *a, **kw: "p")
    monkeypatch.setattr(pages_workspace.runs, "list_runs", lambda *a: [dict(
        id="r", name="demo", state="SUCCEEDED", created_at=None)])
    monkeypatch.setattr(pages_workspace.runs, "get_run", lambda *a: dict(revision=3))
    monkeypatch.setattr(pages_workspace.runs, "request_checks_only", lambda *a: "j")

    async def io_bound(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(pages_workspace.background, "io_bound", io_bound)
    pages_workspace.workspace_page()
    asyncio.run(click(page_client, "Выполнить на прогоне"))
    button = next(e for e in page_client.elements.values()
                  if getattr(e, "text", "") == "Открыть отчёт")
    assert not button.visible
    assert timers[0][0] == 1.5

    def unpublished(*args):
        raise InvalidRequest("ещё не опубликована")

    monkeypatch.setattr(report, "published_check_report", unpublished)
    asyncio.run(timers[0][1]())
    assert not button.visible
    monkeypatch.setattr(report, "published_check_report", lambda *a: check_view(4))
    asyncio.run(timers[0][1]())
    assert button.visible
    assert "Опубликована ревизия 4" in texts(page_client)
