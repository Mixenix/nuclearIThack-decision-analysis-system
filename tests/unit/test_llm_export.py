"""Unit tests for the standalone HTML report and its LLM block (PLAN.md §17.2/§17.3).

Hermetic: :func:`render_report` is a pure function over already-computed
structures, so the document can be checked without a database.

What is asserted — the things the task card grades:

* every user-provided value is escaped (no ``<script>``/``<img onerror>`` survives);
* the file is self-contained: inline CSS only, no ``<script>``, no ``<link>``,
  no ``@import``, no external URL of any kind;
* every link back to the application is absolute and uses ``PUBLIC_BASE_URL``
  (and an application id that is not ID-shaped never becomes an href);
* a LOW-confidence FAIL is pale **and** carries the words "низкая уверенность",
  while a HIGH FAIL is marked loudly — colour is never the only signal;
* the LLM block either shows a real stored opinion with its evidence links, or
  says honestly «недоступно / ошибка <причина>», or «не запрашивалось».

Run: ``app/.venv/bin/python -m pytest app/tests/unit -q``
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from change_analysis.reports.export import (  # noqa: E402
    _llm_view,
    is_safe_app_id,
    render_report,
)
from change_analysis.services.errors import InvalidRequest  # noqa: E402

BASE = "http://192.168.31.231:8000"
RUN_ID = "818573ab-d5a9-49c6-8c60-5b67e8d782a4"
GENERATED = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def run_fixture(**overrides) -> dict:
    run = {
        "id": RUN_ID,
        "name": "Полный прогон",
        "state": "SUCCEEDED",
        "revision": 1,
        "backend": "cpu",
        "expected_pairs": 2002,
        "dataset_name": "RUN-20260827-0001",
        "dataset_root": "RUN-20260827-0001",
        "created_at": datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc),
    }
    run.update(overrides)
    return run


def summary_fixture() -> dict:
    return {
        "cards": [
            {"id": "total_pairs", "label": "Пар заявок в выборке", "value": 2002},
            {"id": "changed_apps", "label": "Заявки с существенными различиями", "value": 502},
            {"id": "decision_flips", "label": "Заявки со сменой решения", "value": 385},
            {"id": "approval_share", "label": "Доля одобрений по заявкам",
             "denominator": 2002, "before_pct": 32.82, "after_pct": 24.78,
             "delta_pp": -8.04},
            {"id": "checks", "label": "Срабатывания проверок", "value": 32,
             "high_fail": 31, "low_fail": 1, "technical_errors": 0},
            {"id": "top_attributes", "label": "Чаще изменяемые атрибуты",
             "items": [{"path": "decisionResult.decision", "cnt": 385}]},
            {"id": "causes", "label": "Признаки источника изменений",
             "items": [{"label": "Логика стратегии", "value": 335}]},
        ],
        "total_pairs": 2002,
        "changed_apps": 502,
        "decision_flips": 385,
        "conclusion": "В выборке 2002 пар заявок.",
        "limitations": ["Два среза ответов не доказывают причину изменения решения."],
    }


def checks_fixture(label: str = "Изменился состав атрибутов",
                   argument: str = "decisionResult.available_offers.product_id",
                   **overrides) -> dict:
    row = {
        "LABEL": label,
        "SOURCE": "BEFORE",
        "ARGUMENT": argument,
        "CNT": 273,
        "TOTAL": 2002,
        "METRIC": None,
        "APP_IDS": ["I32554362"],
        "RESULT": "FAIL",
        "CONFIDENCE": "HIGH",
        "execution_state": "DONE",
        "execution_error": "",
        "app_ids_are_sample": True,
    }
    row.update(overrides)
    return {"revision": 1, "rows": [row], "errors": [], "counts": {
        "rows": 1, "high_fail": 1, "low_fail": 0, "passed": 0, "errors": 0}}


def llm_done_fixture(app_id: str = "I32554362") -> dict:
    stored = {
        "state": "DONE",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "response_model": "deepseek-flash",
        "payload_hash": "a63e4c0a556f97e7",
        "latency_ms": 7389,
        "created_at": datetime(2026, 11, 7, 8, 41, tzinfo=timezone.utc),
        "usage": {"prompt_tokens": 7841, "completion_tokens": 731},
        "summary": "Изменено 502 заявки, решение перевернулось у 385.",
        "observations": [{"text": "Стоп-фактор AGELOW@21 исчез", "evidence_id": "E1"}],
        "uncertainty": ["нет полного списка заявок по каждой проверке"],
        "next_steps": ["свести 385 переворотов с cause_categories"],
        "dropped_evidence": 0,
        "evidence": [{
            "evidence_id": "E1", "app_id": app_id,
            "path": "decisionResult.product_trials.rejectReasonCode",
            "array_key": "15210|standard", "before": "AGELOW@21", "after": "NULL",
            "change_type": "VALUE_CHANGED", "apps_with_this_path": 56,
        }],
        "error": "",
    }
    return _llm_view(stored)


def render(llm=None, llm_status=None, checks=None, run=None, configuration=None) -> str:
    return render_report(
        run or run_fixture(),
        summary_fixture(),
        checks or checks_fixture(),
        BASE,
        generated_at=GENERATED,
        llm=llm,
        llm_status=llm_status,
        configuration=configuration,
    )


# ---------------------------------------------------------------------------
# escaping
# ---------------------------------------------------------------------------


def test_user_values_are_escaped_everywhere():
    hostile = '<script>alert("x")</script>'
    html = render(
        checks=checks_fixture(label=hostile, argument='"><img src=x onerror=alert(1)>'),
        llm=llm_done_fixture(),
        run=run_fixture(name=f"прогон {hostile}"),
    )
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    # the words survive as *text* only — never inside a tag or an attribute
    assert not re.search(r"<[^>]*onerror", html)


def test_llm_text_is_escaped_too():
    stored = llm_done_fixture()
    stored["summary"] = "модель сказала <script>evil()</script>"
    stored["observations"] = [{
        "text": "<b>жирный?</b> <iframe src=//evil></iframe>",
        "evidence_id": "E1",
        "evidence": {"evidence_id": "E1", "app_id": "I32554362",
                     "path": "decisionResult.decision", "array_key": "",
                     "before": "REJECT", "after": "APPROVE",
                     "change_type": "VALUE_CHANGED", "apps_with_this_path": 385},
    }]
    html = render(llm=stored, llm_status={"available": True, "provider": "deepseek",
                                         "model": "deepseek-chat", "reason": "ok"})
    assert "<script" not in html
    assert "<iframe" not in html
    assert "&lt;b&gt;жирный?&lt;/b&gt;" in html


def test_app_id_that_is_not_id_shaped_never_becomes_a_link():
    stored = llm_done_fixture(app_id='X" onmouseover="alert(1)')
    html = render(llm=stored)
    assert f'href="{BASE}/runs/{RUN_ID}/app/X' not in html
    assert not re.search(r"<a [^>]*onmouseover", html)
    assert is_safe_app_id('X" onmouseover="alert(1)') is False
    assert is_safe_app_id("I32554362") is True
    assert is_safe_app_id("") is False


# ---------------------------------------------------------------------------
# self-contained document: inline CSS only
# ---------------------------------------------------------------------------


def test_document_has_no_external_resources():
    html = render(llm=llm_done_fixture(), llm_status={"available": True})
    for forbidden in ("<script", "<link", "@import", "url(http", "src=",
                      "fonts.googleapis", "cdn.", "javascript:"):
        assert forbidden not in html, f"external/active resource found: {forbidden}"
    assert "<style>" in html


def test_every_link_is_absolute_and_uses_public_base_url():
    html = render(llm=llm_done_fixture(), llm_status={"available": True})
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs, "the report must link back into the application"
    for href in hrefs:
        assert href.startswith(BASE), href
    assert "127.0.0.1" not in html and "localhost" not in html
    assert f"{BASE}/runs/{RUN_ID}" in html
    assert f"{BASE}/runs/{RUN_ID}/report" in html
    assert f"{BASE}/runs/{RUN_ID}/explore" in html
    assert f"{BASE}/runs/{RUN_ID}/app/I32554362" in html


def test_relative_base_url_is_rejected():
    with pytest.raises(InvalidRequest):
        render_report(run_fixture(), summary_fixture(), checks_fixture(), "/runs/x")


# ---------------------------------------------------------------------------
# verdict rendering: HIGH bright, LOW pale + words
# ---------------------------------------------------------------------------


def test_high_fail_is_loud_low_fail_is_pale_with_words():
    checks = checks_fixture()
    low_row = dict(checks["rows"][0], CONFIDENCE="LOW", CNT=5, RESULT="FAIL",
                   LABEL="Стоп-фактор исчез")
    checks["rows"].append(low_row)
    html = render(checks=checks)

    assert 'class="fail-high"' in html
    assert 'class="fail-low"' in html
    low_block = html[html.index('class="fail-low"'):]
    low_block = low_block[:low_block.index("</tr>")]
    assert "низкая уверенность" in low_block
    high_block = html[html.index('class="fail-high"'):]
    high_block = high_block[:high_block.index("</tr>")]
    assert "FAIL" in high_block


def test_technical_error_row_is_not_a_business_fail():
    checks = checks_fixture()
    checks["rows"][0].update({"execution_state": "ERROR", "RESULT": ""})
    checks["errors"] = [{"check_id": "my_check", "error": "ValueError: Mart is empty"}]
    html = render(checks=checks)
    assert "Технические ошибки исполнения" in html
    assert "ОШИБКА ИСПОЛНЕНИЯ" in html
    assert "ValueError: Mart is empty" in html


# ---------------------------------------------------------------------------
# the LLM block: three honest states
# ---------------------------------------------------------------------------


def test_llm_done_block_shows_summary_evidence_and_disclaimer():
    html = render(llm=llm_done_fixture(),
                  llm_status={"available": True, "provider": "deepseek",
                              "model": "deepseek-chat", "reason": "ok"})
    assert "Независимое мнение ИИ" in html
    assert "Изменено 502 заявки" in html
    assert "Стоп-фактор AGELOW@21 исчез" in html
    assert f'{BASE}/runs/{RUN_ID}/app/I32554362' in html     # clickable evidence
    assert "deepseek-flash" in html and "7389 мс" in html
    assert "a63e4c0a556f97e7" in html
    assert "не меняет ни одного числа" in html


def test_llm_observation_without_matching_evidence_is_not_linked():
    stored = llm_done_fixture()
    stored["observations"] = [{"text": "наблюдение", "evidence_id": "E99", "evidence": None}]
    html = render(llm=stored)
    assert "evidence <code>E99</code>" in html
    assert f"{BASE}/runs/{RUN_ID}/app/E99" not in html


def test_llm_unavailable_state_is_explicit_and_keeps_deterministic_report():
    stored = _llm_view({
        "state": "UNAVAILABLE",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "error": "переменная окружения DEEPSEEK_API_KEY не задана или пуста",
    })
    html = render(llm=stored,
                  llm_status={"available": False, "provider": "deepseek",
                              "model": "deepseek-chat",
                              "reason": "переменная окружения DEEPSEEK_API_KEY не задана"})
    assert "Мнение ИИ недоступно" in html
    assert "UNAVAILABLE" in html
    assert "DEEPSEEK_API_KEY" in html
    assert "2002" in html and "502" in html      # the deterministic part is untouched


def test_llm_error_state_shows_the_real_reason():
    stored = _llm_view({
        "state": "ERROR",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "error": "HTTP 401 от https://api.deepseek.com/v1/chat/completions: "
                 '{"error": {"message": "Authentication Fails"}}',
    })
    html = render(llm=stored, llm_status={"available": True, "provider": "deepseek",
                                         "model": "deepseek-chat", "reason": "ok"})
    assert "Мнение ИИ недоступно" in html
    assert "HTTP 401" in html
    assert "Authentication Fails" in html


def test_llm_not_requested_state_is_explicit():
    html = render(llm=None,
                  llm_status={"available": False, "provider": "deepseek",
                              "model": "deepseek-chat",
                              "reason": "провайдер не настроен (LLM_PROVIDER пуст)"})
    assert "Мнение ИИ не запрашивалось" in html
    assert "LLM_PROVIDER" in html


def test_llm_not_requested_but_configured_says_how_to_run_it():
    html = render(llm=None,
                  llm_status={"available": True, "provider": "deepseek",
                              "model": "deepseek-chat", "reason": "ключ найден"})
    assert "Мнение ИИ не запрашивалось" in html
    assert "deepseek / deepseek-chat" in html


# ---------------------------------------------------------------------------
# configuration block: what was configured, never the source
# ---------------------------------------------------------------------------


def test_configuration_block_shows_settings_but_no_code():
    configuration = {
        "procedures": {"normalize": {"source_kind": "file",
                                     "source_ref": "spr_normalize.py", "params": {}}},
        "configured_checks": [{"id": "attribute_new_loss",
                               "label": "Изменился состав атрибутов",
                               "source_kind": "file",
                               "source_ref": "check_attribute_new_loss.py",
                               "params": {"app_ids_limit": 20}}],
        "slices": {"BEFORE": "MASTER", "AFTER": "BETA"},
        "technical_fields": ["appData.request_id"],
    }
    html = render(configuration=configuration)
    assert "Конфигурация прогона" in html
    assert "spr_normalize.py" in html
    assert "MASTER = BEFORE" in html or "BEFORE = MASTER" in html
    assert "app_ids_limit" in html and "20" in html
    assert "Исходный код процедур и проверок в отчёт не попадает" in html


def test_missing_configuration_is_reported_not_invented():
    html = render(configuration=None)
    assert "Конфигурация прогона недоступна" in html


def test_failed_later_attempt_is_disclosed_next_to_the_good_answer():
    stored = llm_done_fixture()
    stored["last_attempt"] = {"state": "ERROR", "error": "HTTP 429: rate limited",
                              "at": "2026-11-07 09:00:00 MSK"}
    html = render(llm=stored)
    assert "Последняя попытка" in html
    assert "HTTP 429" in html
    assert "более ранний успешный ответ" in html
    assert "Изменено 502 заявки" in html          # the good answer is still shown
