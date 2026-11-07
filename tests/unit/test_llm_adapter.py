"""Unit tests for the LLM second-opinion adapter (PLAN.md §17.3).

These tests are hermetic: no PostgreSQL connection, no network.  The database and
the HTTP transport are replaced with recorders, so the assertions are about the
*contract* of the block:

* no key / no provider ⇒ ``UNAVAILABLE`` with a truthful reason, and **no HTTP
  request is attempted** (an answer is never invented);
* the payload is bounded, deterministic, carries evidence ids, and never contains
  source code, the database URL or the API key;
* an observation citing an ``evidence_id`` that was not sent is dropped and counted;
* HTTP failures and truncated answers produce ``ERROR`` with the real reason;
* every explicit request is persisted (state included).

Run: ``app/.venv/bin/python -m pytest app/tests/unit -q``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from change_analysis.config import Settings  # noqa: E402
from change_analysis.llm import adapter as llm  # noqa: E402

RUN_ID = "818573ab-d5a9-49c6-8c60-5b67e8d782a4"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def fake_settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql://user:secret-password@127.0.0.1:55432/db",
        dataset_root=Path("/tmp/dataset"),
        script_root=Path("/tmp/scripts"),
        artifact_root=Path("/tmp/artifacts"),
        compute_python="python3",
        llm_provider="deepseek",
        llm_model="deepseek-chat",
        llm_api_key_env="DEEPSEEK_API_KEY",
        llm_base_url="https://api.deepseek.com/v1",
        llm_timeout_sec=60,
        llm_max_payload_chars=24000,
    )
    base.update(overrides)
    return Settings(**base)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Replacement for ``httpx.Client`` that records the outgoing request."""

    response: FakeResponse = FakeResponse()
    last_request: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        self.init_kwargs = kwargs

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def post(self, url, headers=None, content=None) -> FakeResponse:
        FakeClient.last_request = {
            "url": url,
            "headers": headers or {},
            "body": json.loads(content) if content else {},
        }
        return FakeClient.response


def api_answer(summary: str = "сводка", observations=None,
               uncertainty=None, next_steps=None) -> dict:
    """A well-formed OpenAI-compatible response with a JSON-object content."""
    content = {
        "summary": summary,
        "observations": observations if observations is not None
        else [{"text": "наблюдение", "evidence_id": "E1"}],
        "uncertainty": uncertainty if uncertainty is not None else ["мало данных"],
        "next_steps": next_steps if next_steps is not None else ["проверить вручную"],
    }
    return {
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 7000, "completion_tokens": 700, "total_tokens": 7700},
    }


def fake_packet(evidence_count: int = 3) -> dict:
    evidence = [
        {"evidence_id": f"E{index}", "app_id": f"I325543{index:02d}",
         "path": "decisionResult.decision", "array_key": "",
         "before": "REJECT", "after": "APPROVE", "change_type": "VALUE_CHANGED",
         "apps_with_this_path": 385}
        for index in range(1, evidence_count + 1)
    ]
    return {
        "run": {"run_id": RUN_ID, "revision": 1, "backend": "cpu", "state": "SUCCEEDED",
                "expected_pairs": 2002},
        "sample": {"pairs": 2002, "changed_apps": 502, "decision_flips": 385},
        "configuration": {"procedures": {}, "configured_checks": [], "slices": {},
                          "technical_fields": []},
        "top_changed_attributes": [{"path": "decisionResult.decision", "apps": 385}],
        "checks": [{"label": "Изменился состав атрибутов", "argument": "x",
                    "source": "BEFORE", "cnt": 273, "total": 2002, "result": "FAIL",
                    "confidence": "HIGH", "execution_state": "DONE",
                    "execution_error": ""}],
        "check_counts": {"rows": 1, "high_fail": 1, "low_fail": 0, "passed": 0, "errors": 0},
        "flips_by_cause": [{"cause": "LOGIC", "flips": 218}],
        "decision_changes": [{"app_id": "I32554354", "decision": "REJECT → APPROVE"}],
        "evidence": evidence,
        "field_dictionary": llm.FIELD_DICTIONARY,
        "limitations": llm.LIMITATIONS,
        "expected_response": llm.RESPONSE_SCHEMA_HINT,
    }


@pytest.fixture
def recorded_persists(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def record(run_id, revision, result):
        calls.append({"run_id": run_id, "revision": revision, "result": result})
        return ""

    monkeypatch.setattr(llm, "_persist", record)
    return calls


# ---------------------------------------------------------------------------
# status(): the app must be honest when nothing is configured
# ---------------------------------------------------------------------------


def test_status_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    state = llm.status()
    assert state["available"] is False
    assert state["provider"] == "deepseek"
    assert "DEEPSEEK_API_KEY" in state["reason"]


def test_status_without_provider_is_unavailable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "load_settings",
                        lambda: fake_settings(llm_provider="", llm_model=""))
    state = llm.status()
    assert state["available"] is False
    assert "LLM_PROVIDER" in state["reason"]


def test_status_without_api_key_env_name_is_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings(llm_api_key_env=""))
    state = llm.status()
    assert state["available"] is False
    assert "LLM_API_KEY_ENV" in state["reason"]


def test_status_rejects_relative_base_url(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "load_settings",
                        lambda: fake_settings(llm_base_url="api.deepseek.com/v1"))
    state = llm.status()
    assert state["available"] is False
    assert "HTTP" in state["reason"]


def test_status_available_reports_provider_model_and_timeout(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    state = llm.status()
    assert state == {"available": True, "provider": "deepseek", "model": "deepseek-chat",
                     "timeout_sec": 60,
                     "reason": "ключ найден в DEEPSEEK_API_KEY, timeout 60 с"}


# ---------------------------------------------------------------------------
# payload: bounded, deterministic, no secrets, no source code
# ---------------------------------------------------------------------------


SNAPSHOT = {
    "backend": "cpu",
    "procedures": {
        "normalize": {"source_kind": "file", "source_ref": "/home/u/app/src/procedures/spr_normalize.py",
                      "params": {}, "code": "SECRET-PROCEDURE-SOURCE"},
    },
    "checks": [{"id": "attribute_new_loss", "label": "Изменился состав атрибутов",
                "source_kind": "file", "source_ref": "/srv/scripts/check_attribute_new_loss.py",
                "params": {"app_ids_limit": 20}, "code": "SECRET-CHECK-SOURCE"}],
    "slices": {"BEFORE": {"label": "MASTER", "dir": "RESULTS/BEFORE_MASTER"},
               "AFTER": {"label": "BETA", "dir": "RESULTS/AFTER_BETA"}},
    "technical_fields": ["appData.request_id", "appData.createDateTime"],
}


def patch_payload_sources(monkeypatch, evidence_rows: int = 3):
    from change_analysis.db import connection
    from change_analysis.llm import summary_light
    from change_analysis.services import checks as checks_service

    def fake_query(sql: str, params=()):
        if "FROM ca.runs" in sql:
            return [{"state": "SUCCEEDED", "backend": "cpu", "expected_pairs": 2002,
                     "snapshot": SNAPSHOT}]
        if "flips_by_cause" in sql or "decision_changed = 1 GROUP BY" in sql:
            return [{"cause": "LOGIC", "flips": 218}, {"cause": "MIXED", "flips": 167}]
        if "FROM ca.app_segments" in sql:
            return [{"app_id": "I32554354", "decision_before": "REJECT",
                     "decision_after": "APPROVE", "cause_category": "LOGIC",
                     "pd_model_before": "ML_MICRO_LOAN_V1",
                     "pd_model_after": "ML_MICRO_LOAN_V3",
                     "cutoff_before": "0.55", "cutoff_after": "0.62",
                     "top_offer_before": "", "top_offer_after": "15210",
                     "products": "15210;5123"}]
        if "ranked" in sql:
            limit = int((params or {}).get("limit") or evidence_rows)
            return [{"app_id": f"I3255435{i}", "path": "decisionResult.decision",
                     "array_key": "", "value_before": "REJECT", "value_after": "APPROVE",
                     "change_type": "VALUE_CHANGED", "path_apps": 385}
                    for i in range(min(evidence_rows, limit))]
        if "GROUP BY path" in sql:
            return [{"path": "decisionResult.decision", "apps": 385},
                    {"path": "decisionResult.available_offers.product_id", "apps": 385}]
        return []

    monkeypatch.setattr(connection, "query", fake_query)
    monkeypatch.setattr(summary_light, "light_summary", lambda run_id, revision: {
        "pairs": 2002, "changed_apps": 502, "changed_share": 0.2507, "decision_flips": 385,
        "approval_rate_before": 32.82, "approval_rate_after": 24.78,
        "rows": {"VALUE_CHANGED": 7567, "ATTRIBUTE_NEW": 3835, "ATTRIBUTE_LOST": 3010},
        "cause_categories": {"NONE": 1500, "LOGIC": 335, "MIXED": 167}, "revision": 1,
    })
    monkeypatch.setattr(checks_service, "build_check_report", lambda *a, **k: {
        "rows": [{"LABEL": "Изменился состав атрибутов", "ARGUMENT": "decisionResult.x",
                  "SOURCE": "BEFORE", "CNT": 273, "TOTAL": 2002, "METRIC": None,
                  "RESULT": "FAIL", "CONFIDENCE": "HIGH", "execution_state": "DONE",
                  "execution_error": "", "APP_IDS": ["I32554354"]}],
        "counts": {"rows": 1, "high_fail": 1, "low_fail": 0, "passed": 0, "errors": 0},
        "errors": [],
    })


def test_payload_carries_no_source_code_and_no_absolute_paths(monkeypatch):
    patch_payload_sources(monkeypatch)
    packet = llm.build_payload(RUN_ID, 1)
    text = json.dumps(packet, ensure_ascii=False)

    assert "SECRET-PROCEDURE-SOURCE" not in text
    assert "SECRET-CHECK-SOURCE" not in text
    assert '"code"' not in text                      # no source key at all
    assert "/home/u/app/src/procedures" not in text  # absolute paths trimmed
    assert packet["configuration"]["procedures"]["normalize"]["source_ref"] == "spr_normalize.py"
    assert packet["configuration"]["configured_checks"][0]["source_ref"] == \
        "check_attribute_new_loss.py"


def test_payload_is_deterministic_and_evidence_ids_are_sequential(monkeypatch):
    patch_payload_sources(monkeypatch, evidence_rows=5)
    first = llm.build_payload(RUN_ID, 1)
    second = llm.build_payload(RUN_ID, 1)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    ids = [item["evidence_id"] for item in first["evidence"]]
    assert ids == ["E1", "E2", "E3", "E4", "E5"]
    assert len(set(ids)) == len(ids)


def test_payload_respects_evidence_limit_from_environment(monkeypatch):
    patch_payload_sources(monkeypatch, evidence_rows=40)
    monkeypatch.setenv("LLM_MAX_EVIDENCE", "3")
    packet = llm.build_payload(RUN_ID, 1)
    assert [item["evidence_id"] for item in packet["evidence"]] == ["E1", "E2", "E3"]


def test_max_evidence_is_clamped(monkeypatch):
    monkeypatch.setenv("LLM_MAX_EVIDENCE", "100000")
    assert llm._max_evidence() == llm.MAX_EVIDENCE_CEILING
    monkeypatch.setenv("LLM_MAX_EVIDENCE", "не число")
    assert llm._max_evidence() == llm.DEFAULT_MAX_EVIDENCE
    monkeypatch.setenv("LLM_MAX_EVIDENCE", "-5")
    assert llm._max_evidence() == 1
    monkeypatch.delenv("LLM_MAX_EVIDENCE")
    assert llm._max_evidence() == llm.DEFAULT_MAX_EVIDENCE


def test_payload_never_contains_the_api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-secret-value-0123456789")
    patch_payload_sources(monkeypatch)
    text = json.dumps(llm.build_payload(RUN_ID, 1), ensure_ascii=False)
    assert "sk-test-secret-value-0123456789" not in text
    assert "DEEPSEEK_API_KEY" not in text
    assert "secret-password" not in text             # no DSN either


def test_redaction_masks_secret_shaped_values():
    dirty = {"value": "sk-abcdefghijklmnop", "note": "password=hunter2",
             "header": "Bearer abcdefghijklmnop", "keep": "I32554362"}
    clean = llm._redact(dirty)
    assert clean["value"] == "[скрыто]"
    assert "hunter2" not in clean["note"]
    assert "[скрыто]" in clean["note"]
    assert "abcdefghijklmnop" not in clean["header"]
    assert clean["keep"] == "I32554362"


def test_trim_shrinks_the_packet_to_the_budget():
    packet = fake_packet(evidence_count=40)
    size_before = len(json.dumps(packet, ensure_ascii=False))
    trimmed, size, dropped, notes = llm._trim(packet, 4000)
    assert size <= 4000
    assert size < size_before
    assert dropped > 0
    assert trimmed["evidence"]                          # at least one row survives
    assert any("урезана" in note for note in notes)


# ---------------------------------------------------------------------------
# response validation: an invented evidence id is dropped, not displayed
# ---------------------------------------------------------------------------


def test_observation_with_unknown_evidence_id_is_dropped():
    parsed = {
        "summary": "сводка",
        "observations": [
            {"text": "ссылка на существующее", "evidence_id": "E1"},
            {"text": "ссылка на несуществующее", "evidence_id": "E99"},
            "наблюдение без ссылки",
        ],
        "uncertainty": ["мало данных"],
        "next_steps": ["проверить"],
    }
    validated = llm._validate_response(parsed, {"E1"})
    assert validated["schema_ok"] is True
    assert validated["dropped_evidence"] == 1
    texts = [item["text"] for item in validated["observations"]]
    assert "ссылка на существующее" in texts
    assert "наблюдение без ссылки" in texts
    assert "ссылка на несуществующее" not in texts


def test_response_without_known_keys_is_not_a_valid_answer():
    validated = llm._validate_response({"answer": "42"}, {"E1"})
    assert validated["schema_ok"] is False


def test_json_object_parser_tolerates_fences_but_rejects_prose():
    assert llm._parse_json_object('```json\n{"summary": "x"}\n```') == {"summary": "x"}
    assert llm._parse_json_object('Вот ответ: {"summary": "x"} — готово') == {"summary": "x"}
    assert llm._parse_json_object("обычный текст") is None
    assert llm._parse_json_object("") is None


# ---------------------------------------------------------------------------
# request_second_opinion: no key ⇒ UNAVAILABLE, no HTTP at all
# ---------------------------------------------------------------------------


def test_request_without_key_makes_no_http_call_and_is_stored(monkeypatch, recorded_persists):
    import httpx

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("no HTTP request may happen without a key")

    monkeypatch.setattr(httpx, "Client", explode)
    result = llm.request_second_opinion(RUN_ID, 1, {})

    assert result["state"] == "UNAVAILABLE"
    assert "DEEPSEEK_API_KEY" in result["error"]
    assert result["observations"] == [] and result["summary"] == ""
    assert len(recorded_persists) == 1
    assert recorded_persists[0]["result"]["state"] == "UNAVAILABLE"
    assert recorded_persists[0]["run_id"] == RUN_ID


def test_request_without_key_but_with_stored_answer_shows_reason(monkeypatch):
    """status() must explain the absence even if an old opinion exists."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    state = llm.status()
    assert state["available"] is False and state["reason"]


# ---------------------------------------------------------------------------
# request_second_opinion: the happy path, with a recorded request/response
# ---------------------------------------------------------------------------


def test_request_done_payload_and_result(monkeypatch, recorded_persists):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-abcdefghijkl")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    seen_extra: list[dict] = []

    def fake_build(run_id, revision, extra):
        seen_extra.append(dict(extra or {}))
        packet = fake_packet(3)
        if extra:
            packet["extra_context"] = llm._clip_json(llm._redact(extra), llm.MAX_EXTRA_CHARS)
        return packet

    monkeypatch.setattr(llm, "build_payload", fake_build)

    FakeClient.response = FakeResponse(200, api_answer(
        observations=[{"text": "существующее evidence", "evidence_id": "E2"},
                      {"text": "выдуманное evidence", "evidence_id": "E77"}]))
    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = llm.request_second_opinion(RUN_ID, 1, {"note": "из UI"})

    assert result["state"] == "DONE"
    assert result["provider"] == "deepseek" and result["model"] == "deepseek-chat"
    assert result["response_model"] == "deepseek-flash"
    assert result["dropped_evidence"] == 1
    assert [item["evidence_id"] for item in result["observations"]] == ["E2"]
    assert result["payload_hash"] and len(result["payload_hash"]) == 16

    request = FakeClient.last_request
    assert request["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-test-key-abcdefghijkl"
    assert request["body"]["model"] == "deepseek-chat"
    assert request["body"]["response_format"] == {"type": "json_object"}
    assert request["body"]["stream"] is False
    assert request["body"]["max_tokens"] == llm.MAX_RESPONSE_TOKENS
    sent_packet = json.loads(request["body"]["messages"][1]["content"])
    assert [item["evidence_id"] for item in sent_packet["evidence"]] == ["E1", "E2", "E3"]
    assert sent_packet["extra_context"] == {"note": "из UI"}
    assert seen_extra == [{"note": "из UI"}]

    # the key may travel in the header only — never in the stored result
    assert "sk-test-key-abcdefghijkl" not in json.dumps(result, ensure_ascii=False)
    assert len(recorded_persists) == 1
    stored = recorded_persists[0]["result"]
    assert stored["state"] == "DONE" and stored["latency_ms"] >= 0


def test_request_http_error_reports_real_status_and_text(monkeypatch, recorded_persists):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-abcdefghijkl")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    monkeypatch.setattr(llm, "build_payload", lambda run_id, revision, extra: fake_packet(2))

    FakeClient.response = FakeResponse(401, {"error": {"message": "Authentication Fails"}},
                                      text='{"error": {"message": "Authentication Fails"}}')
    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = llm.request_second_opinion(RUN_ID, 1, {})
    assert result["state"] == "ERROR"
    assert result["http_status"] == 401
    assert "HTTP 401" in result["error"]
    assert "Authentication Fails" in result["error"]
    assert recorded_persists[0]["result"]["state"] == "ERROR"


def test_request_network_failure_is_error_not_a_fabricated_answer(monkeypatch, recorded_persists):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-abcdefghijkl")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    monkeypatch.setattr(llm, "build_payload", lambda run_id, revision, extra: fake_packet(2))

    class TimeoutClient(FakeClient):
        def post(self, url, headers=None, content=None):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "Client", TimeoutClient)

    result = llm.request_second_opinion(RUN_ID, 1, {})
    assert result["state"] == "ERROR"
    assert "ConnectTimeout" in result["error"]
    assert result["summary"] == "" and result["observations"] == []


def test_truncated_answer_is_reported_as_length_not_as_json_noise(monkeypatch,
                                                                  recorded_persists):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-abcdefghijkl")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())
    monkeypatch.setattr(llm, "build_payload", lambda run_id, revision, extra: fake_packet(2))

    truncated = {"model": "deepseek-flash", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": '{"summary": "Прогон 818'},
         "finish_reason": "length"}],
        "usage": {"prompt_tokens": 7000, "completion_tokens": 900}}
    FakeClient.response = FakeResponse(200, truncated)
    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = llm.request_second_opinion(RUN_ID, 1, {})
    assert result["state"] == "ERROR"
    assert result["finish_reason"] == "length"
    assert "обрезан" in result["error"]


def test_payload_build_failure_is_reported_not_raised(monkeypatch, recorded_persists):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-abcdefghijkl")
    monkeypatch.setattr(llm, "load_settings", lambda: fake_settings())

    def boom(run_id, revision, extra):
        raise ValueError("прогон не найден")

    monkeypatch.setattr(llm, "build_payload", boom)
    result = llm.request_second_opinion(RUN_ID, 1, {})
    assert result["state"] == "ERROR"
    assert result["stage"] == "build_payload"
    assert "прогон не найден" in result["error"]
    assert recorded_persists and recorded_persists[0]["result"]["state"] == "ERROR"


# ---------------------------------------------------------------------------
# load_report: append-only storage must not let a failed retry hide a good answer
# ---------------------------------------------------------------------------


def _stored_row(row_id: int, state: str, summary: str = "", error: str = "") -> dict:
    return {
        "id": row_id, "revision": 1, "provider": "deepseek", "model": "deepseek-chat",
        "payload_hash": "abc123", "state": state,
        "result": {"summary": summary, "observations": [], "uncertainty": [],
                   "next_steps": [], "evidence": []},
        "error": error, "created_at": f"2026-11-07 0{row_id}:00",
        "finished_at": f"2026-11-07 0{row_id}:00",
    }


def test_load_report_prefers_the_last_successful_attempt(monkeypatch):
    from change_analysis.db import connection

    failed = _stored_row(3, "ERROR", error="HTTP 429: rate limited")
    done = _stored_row(2, "DONE", summary="успешное мнение")

    def fake_query(sql, params=()):
        return [done] if "AND state = 'DONE'" in sql else [failed]

    monkeypatch.setattr(connection, "query", fake_query)
    report = llm.load_report(RUN_ID, 1)

    assert report is not None
    assert report["state"] == "DONE"
    assert report["summary"] == "успешное мнение"
    assert report["last_attempt"]["state"] == "ERROR"
    assert "HTTP 429" in report["last_attempt"]["error"]


def test_load_report_returns_the_failed_attempt_when_nothing_ever_succeeded(monkeypatch):
    from change_analysis.db import connection

    failed = _stored_row(3, "UNAVAILABLE", error="переменная окружения пуста")

    def fake_query(sql, params=()):
        return [] if "AND state = 'DONE'" in sql else [failed]

    monkeypatch.setattr(connection, "query", fake_query)
    report = llm.load_report(RUN_ID, 1)
    assert report is not None
    assert report["state"] == "UNAVAILABLE"
    assert report["last_attempt"] is None
    assert "переменная окружения" in report["error"]


def test_load_report_returns_none_when_never_requested(monkeypatch):
    from change_analysis.db import connection

    monkeypatch.setattr(connection, "query", lambda sql, params=(): [])
    assert llm.load_report(RUN_ID, 1) is None
