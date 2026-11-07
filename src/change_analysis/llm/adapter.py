"""Independent second opinion from a language model (PLAN.md §17.3).

This block is **not** part of any deterministic number.  It never writes to the
mart, never changes a check verdict and never runs when no key is configured: in
that case it reports ``UNAVAILABLE`` with a truthful reason instead of inventing
an answer.

What the model is allowed to see
--------------------------------
A whole pivot table (289k rows) cannot be sent, so the packet is deliberately
bounded and reproducible:

* aggregates of the run (pairs, changed applications, decision flips, approval
  rates, cause categories), computed by :func:`summary_light.light_summary`;
* the merged check report (labels, arguments, CNT/TOTAL, verdict, confidence);
* the most frequently changed attribute paths (top-N distinct applications);
* a **deterministic** sample of at most ``LLM_MAX_EVIDENCE`` difference rows
  (default 40, hard ceiling 200), ordered by path and application id, so the same
  run always produces the same evidence ids (``E1``…``En``);
* a field dictionary and an explicit list of limitations.

It never receives procedure source code, the database URL, session tokens,
passwords or the API key.  Every string is length-clipped and secret-shaped
substrings (``sk-…``, ``Bearer …``, ``password=…``) are masked before the packet
is serialised.  The serialised packet is additionally capped at
``LLM_MAX_PAYLOAD_CHARS`` by dropping evidence rows, then top paths, then checks.

What comes back
---------------
The answer is parsed as JSON and validated: an observation that cites an
``evidence_id`` which is not present in the packet is **dropped** and counted.
Text is never interpreted as HTML and never replaces a deterministic figure.

Where the answer is stored
--------------------------
Every explicit request is appended to ``ca.llm_reports`` (state, provider, model,
payload hash, result, error, timestamps).  Rows are append-only: the report page
and the HTML export read the latest row by ``id DESC``, so one failed attempt does
not erase the previous successful one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import load_settings

SYSTEM_PROMPT = (
    "Ты — независимый ревьюер результатов сравнения двух версий стратегии "
    "принятия решений (СПР). Тебе дают агрегаты прогона, результаты "
    "автоматических проверок, небольшую выборку различий и словарь полей. "
    "Твоя задача — указать, что выглядит подозрительно и на что аналитику стоит "
    "посмотреть. Правила: не выдумывай числа; ссылайся только на evidence_id из "
    "входа; не делай вывода «можно выкатывать»; явно отделяй наблюдение от "
    "гипотезы о причине; если данных мало — скажи это. Отвечай строго "
    "JSON-объектом без пояснений вокруг него. Ограничь объём: summary до 120 слов, "
    "не более 8 наблюдений (observations), не более 5 пунктов uncertainty и "
    "не более 5 next_steps; каждое наблюдение — одна короткая фраза."
)

RESPONSE_SCHEMA_HINT = {
    "summary": "краткий абзац: что произошло в прогоне, только по присланным числам",
    "observations": [{"text": "наблюдение", "evidence_id": "E1"}],
    "uncertainty": ["чего не хватает для уверенного вывода"],
    "next_steps": ["конкретная проверка, которую стоит сделать аналитику"],
}

FIELD_DICTIONARY = {
    "APP_ID": "идентификатор заявки; пара BEFORE/AFTER сравнивается по нему",
    "SOURCE": "срез ответа: BEFORE (прод) или AFTER (изменение)",
    "DIFF_FLAG": "1 — атрибут различается, 0 — совпал",
    "CHANGE_TYPE": "SAME | VALUE_CHANGED | ATTRIBUTE_NEW | ATTRIBUTE_LOST",
    "PATH": "путь атрибута без маркеров массива",
    "ARRAY_KEY": "ключи элементов массивов на пути, соединённые '|'",
    "CNT/TOTAL": "число заявок, попавших в условие проверки, из общего числа",
    "CONFIDENCE": "HIGH — уверенное срабатывание, LOW — слабый сигнал",
}

LIMITATIONS = [
    "Два среза ответов не доказывают причину изменения решения.",
    "Проверки посчитаны по всему прогону и не пересчитаны по фильтрам интерфейса.",
    "APP_IDS в строках проверок — ограниченная выборка, а не полный набор заявок.",
    "Выборка различий детерминированно ограничена и не покрывает витрину целиком.",
    "Модель видит только агрегаты и выборку; её выводы — гипотезы, а не результат проверок.",
]

DEFAULT_MAX_EVIDENCE = 40
MAX_EVIDENCE_CEILING = 200
MAX_TOP_PATHS = 15
MAX_CHECKS = 40
MAX_TEXT = 200
MAX_SUMMARY = 2000
MAX_OBSERVATIONS = 20
MAX_LIST_ITEMS = 10
MAX_EXTRA_CHARS = 2000
# 900 tokens truncated the answer mid-JSON on the 2002-application run (measured:
# completion_tokens == max_tokens, finish_reason "length"), so the budget is
# larger and the prompt asks for a short answer.
MAX_RESPONSE_TOKENS = 2000

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(?:pass(?:word)?|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;\"']+"),
)
_REDACTED = "[скрыто]"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _max_evidence() -> int:
    """``LLM_MAX_EVIDENCE`` — how many difference rows the model may see.

    Read through the environment because ``config.Settings`` has no field for it
    yet; ``config.load_settings()`` copies ``.env`` into ``os.environ``, so a value
    in ``app/.env`` is picked up.  Clamped to a safe range so a typo cannot send
    the whole mart.
    """
    raw = os.environ.get("LLM_MAX_EVIDENCE", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_EVIDENCE
    return max(1, min(value, MAX_EVIDENCE_CEILING))


def _api_key(settings) -> str:
    name = settings.llm_api_key_env
    if not name:
        return ""
    return os.environ.get(name, "").strip()


def _absolute_http_url(value: str) -> bool:
    parsed = urlsplit(value or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def status() -> dict[str, Any]:
    """Is a real integration configured?  No network request is made here."""
    settings = load_settings()
    base = {"provider": settings.llm_provider, "model": settings.llm_model}
    if not settings.llm_provider:
        return {**base, "available": False,
                "reason": "провайдер не настроен (LLM_PROVIDER пуст)"}
    if not settings.llm_api_key_env:
        return {**base, "available": False,
                "reason": "не задано имя переменной окружения с ключом (LLM_API_KEY_ENV)"}
    if not _api_key(settings):
        return {**base, "available": False,
                "reason": f"переменная окружения {settings.llm_api_key_env} не задана или пуста"}
    if not settings.llm_base_url:
        return {**base, "available": False, "reason": "не задан LLM_BASE_URL"}
    if not _absolute_http_url(settings.llm_base_url):
        return {**base, "available": False,
                "reason": "LLM_BASE_URL должен быть абсолютным HTTP(S) URL"}
    if not settings.llm_model:
        return {**base, "available": False, "reason": "не задан LLM_MODEL"}
    return {**base, "available": True,
            "timeout_sec": settings.llm_timeout_sec,
            "reason": f"ключ найден в {settings.llm_api_key_env}, "
                      f"timeout {settings.llm_timeout_sec} с"}


# ---------------------------------------------------------------------------
# sanitising helpers — the packet must stay small and free of secrets
# ---------------------------------------------------------------------------


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def _redact(value: Any) -> Any:
    """Recursively mask secret-looking substrings in every string of the packet."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {_redact_text(str(k)): _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(str(value))


def _clip_json(value: Any, limit: int) -> Any:
    """Keep a small JSON-serialisable value; anything larger becomes a note."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return _clip(value)
    if len(text) <= limit:
        return value
    return {"_truncated": _clip(text, limit)}


def _short_ref(value: Any) -> str:
    """Only the file name of a procedure reference — never an absolute path."""
    text = str(value or "").strip()
    if not text:
        return ""
    return _clip(Path(text).name if ("/" in text or "\\" in text) else text, 80)


# ---------------------------------------------------------------------------
# payload construction
# ---------------------------------------------------------------------------


def config_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What was configured — without a single line of user code.

    The snapshot keeps the procedure source in ``procedures.*.code`` and
    ``checks[].code``; those keys are never copied into the packet.
    """
    procedures: dict[str, Any] = {}
    for stage, spec in (snapshot.get("procedures") or {}).items():
        if not isinstance(spec, dict):
            continue
        procedures[_clip(stage, 40)] = {
            "source_kind": _clip(spec.get("source_kind"), 40),
            "source_ref": _short_ref(spec.get("source_ref")),
            "params": _clip_json(spec.get("params") or {}, 400),
        }
    checks = []
    for spec in (snapshot.get("checks") or [])[:20]:
        if not isinstance(spec, dict):
            continue
        checks.append({
            "id": _clip(spec.get("id"), 80),
            "label": _clip(spec.get("label"), 120),
            "source_kind": _clip(spec.get("source_kind"), 40),
            "source_ref": _short_ref(spec.get("source_ref")),
            "params": _clip_json(spec.get("params") or {}, 400),
        })
    slices = {}
    for name, spec in (snapshot.get("slices") or {}).items():
        if isinstance(spec, dict):
            slices[_clip(name, 20)] = _clip(spec.get("label") or spec.get("dir"), 40)
    return {
        "procedures": procedures,
        "configured_checks": checks,
        "slices": slices,
        "technical_fields": [_clip(item, 120)
                             for item in (snapshot.get("technical_fields") or [])[:10]],
    }


def _fetch_evidence(run_id: str, limit: int) -> list[dict[str, Any]]:
    """A deterministic sample of difference rows, spread over the busiest paths.

    A plain ``ORDER BY path, app_id LIMIT n`` was measured to return rows of a
    *single* attribute (the busiest path takes the whole budget), which left the
    model without evidence for anything else.  Rows are therefore ranked per path
    and taken round-robin, so the packet covers many attributes while staying
    reproducible for the same run.
    """
    from ..db.connection import query

    per_path = max(2, -(-int(limit) // MAX_TOP_PATHS))     # ceil(limit / paths)
    rows = query(
        """
        WITH top AS (
            SELECT path, count(DISTINCT app_id) AS apps
              FROM ca.comparison_rows
             WHERE run_id = %(run)s AND diff_flag = 1
             GROUP BY path
             ORDER BY apps DESC, path
             LIMIT %(paths)s
        ), ranked AS (
            SELECT c.app_id, c.path, c.array_key, c.value_before, c.value_after,
                   c.change_type, t.apps AS path_apps,
                   row_number() OVER (PARTITION BY c.path
                                      ORDER BY c.app_id, c.array_key) AS row_no
              FROM ca.comparison_rows c
              JOIN top t ON t.path = c.path
             WHERE c.run_id = %(run)s AND c.diff_flag = 1
        )
        SELECT app_id, path, array_key, value_before, value_after, change_type, path_apps
          FROM ranked
         WHERE row_no <= %(per_path)s
         ORDER BY path_apps DESC, path, app_id, array_key
         LIMIT %(limit)s
        """,
        {"run": run_id, "paths": MAX_TOP_PATHS, "per_path": per_path,
         "limit": int(limit)},
    )
    evidence: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        evidence.append({
            "evidence_id": f"E{index}",
            "app_id": _clip(row["app_id"], 64),
            "path": _clip(row["path"], 160),
            "array_key": _clip(row["array_key"], 80),
            "before": _clip(row["value_before"], MAX_TEXT),
            "after": _clip(row["value_after"], MAX_TEXT),
            "change_type": _clip(row["change_type"], 32),
            "apps_with_this_path": int(row["path_apps"] or 0),
        })
    return evidence


def _flips_by_cause(run_id: str) -> list[dict[str, Any]]:
    """How decision flips distribute over the observed cause categories."""
    from ..db.connection import query

    rows = query(
        "SELECT coalesce(nullif(cause_category, ''), 'UNKNOWN') AS cause,"
        " count(*) AS flips FROM ca.app_segments"
        " WHERE run_id = %s AND decision_changed = 1 GROUP BY 1 ORDER BY flips DESC",
        (run_id,),
    )
    return [{"cause": _clip(row["cause"], 40), "flips": int(row["flips"])}
            for row in rows]


def _decision_changes(run_id: str, limit: int = 6) -> list[dict[str, Any]]:
    """A few flips with the fields that usually explain them (no personal data)."""
    from ..db.connection import query

    rows = query(
        "SELECT app_id, decision_before, decision_after, cause_category,"
        " pd_model_before, pd_model_after, cutoff_before, cutoff_after,"
        " top_offer_before, top_offer_after, products"
        " FROM ca.app_segments WHERE run_id = %s AND decision_changed = 1"
        " ORDER BY app_id LIMIT %s", (run_id, int(limit)),
    )
    out = []
    for row in rows:
        out.append({
            "app_id": _clip(row["app_id"], 64),
            "decision": f"{_clip(row['decision_before'], 20)} → {_clip(row['decision_after'], 20)}",
            "cause_category": _clip(row["cause_category"], 40),
            "pd_model": f"{_clip(row['pd_model_before'], 60)} → {_clip(row['pd_model_after'], 60)}",
            "cutoff": f"{_clip(row['cutoff_before'], 40)} → {_clip(row['cutoff_after'], 40)}",
            "top_offer": f"{_clip(row['top_offer_before'], 40)} → {_clip(row['top_offer_after'], 40)}",
            "products": _clip(row["products"], 120),
        })
    return out


def build_payload(run_id: str, revision: int,
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The bounded, deterministic packet described in the module docstring."""
    from ..db.connection import query
    from ..services.checks import build_check_report
    from .summary_light import light_summary

    run_rows = query(
        "SELECT state, backend, expected_pairs, snapshot FROM ca.runs WHERE id = %s",
        (run_id,),
    )
    if not run_rows:
        raise ValueError("прогон не найден")
    run = run_rows[0]
    snapshot = run.get("snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            snapshot = {}

    summary = light_summary(run_id, revision)
    checks = build_check_report(run_id, revision)
    top_paths = query(
        "SELECT path, count(DISTINCT app_id) AS apps FROM ca.comparison_rows"
        " WHERE run_id = %s AND diff_flag = 1 GROUP BY path"
        " ORDER BY apps DESC, path LIMIT %s", (run_id, MAX_TOP_PATHS),
    )

    payload: dict[str, Any] = {
        "run": {
            "run_id": str(run_id),
            "revision": int(revision),
            "backend": _clip(run.get("backend"), 20),
            "state": _clip(run.get("state"), 20),
            "expected_pairs": int(run.get("expected_pairs") or 0),
        },
        "sample": summary,
        "configuration": config_digest(snapshot),
        "top_changed_attributes": [
            {"path": _clip(row["path"], 160), "apps": int(row["apps"])}
            for row in top_paths
        ],
        "checks": [
            {
                "label": _clip(row["LABEL"], 160),
                "argument": _clip(row["ARGUMENT"], 160),
                "source": _clip(row["SOURCE"], 20),
                "cnt": int(row["CNT"]),
                "total": int(row["TOTAL"]),
                "result": _clip(row["RESULT"], 16),
                "confidence": _clip(row["CONFIDENCE"], 16),
                "execution_state": _clip(row["execution_state"], 16),
                # a crashed check is visible as an execution problem, never as FAIL
                "execution_error": _clip(row.get("execution_error"), 200),
            }
            for row in checks["rows"][:MAX_CHECKS]
        ],
        "check_counts": dict(checks.get("counts") or {}),
        "flips_by_cause": _flips_by_cause(run_id),
        "decision_changes": _decision_changes(run_id),
        "evidence": _fetch_evidence(run_id, _max_evidence()),
        "field_dictionary": FIELD_DICTIONARY,
        "limitations": LIMITATIONS,
        "expected_response": RESPONSE_SCHEMA_HINT,
    }
    if extra:
        payload["extra_context"] = _clip_json(_redact(extra), MAX_EXTRA_CHARS)
    return _redact(payload)


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _trim(payload: dict[str, Any], limit: int) -> tuple[dict[str, Any], int, int, list[str]]:
    """Shrink the packet until it fits ``limit``; returns (packet, chars, dropped, notes)."""
    notes: list[str] = []
    dropped = 0
    size = _payload_size(payload)

    while size > limit and len(payload.get("evidence") or []) > 1:
        payload["evidence"] = payload["evidence"][:-1]
        dropped += 1
        size = _payload_size(payload)
    if dropped:
        notes.append(f"выборка различий урезана на {dropped} записей до лимита "
                     f"{limit} символов")

    while size > limit and len(payload.get("top_changed_attributes") or []) > 3:
        payload["top_changed_attributes"] = payload["top_changed_attributes"][:-1]
        size = _payload_size(payload)
        if "обрезаны частые атрибуты" not in notes:
            notes.append("обрезаны частые атрибуты")

    while size > limit and len(payload.get("checks") or []) > 5:
        payload["checks"] = payload["checks"][:-1]
        size = _payload_size(payload)
        if "обрезаны строки проверок" not in notes:
            notes.append("обрезаны строки проверок")

    if size > limit:
        notes.append("пакет превышает LLM_MAX_PAYLOAD_CHARS даже после обрезки")
    return payload, size, dropped, notes


# ---------------------------------------------------------------------------
# response parsing and validation
# ---------------------------------------------------------------------------


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Tolerate a fenced code block, but require a JSON object."""
    candidate = (text or "").strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json|JSON)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _string_list(value: Any, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text") or item.get("text_ru") or ""
        text = _clip(item, 400)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _validate_response(parsed: dict[str, Any], known_ids: set[str]) -> dict[str, Any]:
    """Normalise the model answer and drop observations citing unknown evidence."""
    summary = _clip(parsed.get("summary"), MAX_SUMMARY)

    observations: list[dict[str, Any]] = []
    dropped = 0
    raw_observations = parsed.get("observations")
    if isinstance(raw_observations, str):
        raw_observations = [raw_observations]
    if isinstance(raw_observations, (list, tuple)):
        for item in raw_observations:
            if isinstance(item, str):
                item = {"text": item, "evidence_id": ""}
            if not isinstance(item, dict):
                continue
            text = _clip(item.get("text") or item.get("observation") or "", 800)
            evidence_id = _clip(item.get("evidence_id"), 32)
            if evidence_id and evidence_id not in known_ids:
                dropped += 1          # cited evidence that was never sent
                continue
            if not text:
                continue
            observations.append({"text": text, "evidence_id": evidence_id})
            if len(observations) >= MAX_OBSERVATIONS:
                break

    uncertainty = _string_list(parsed.get("uncertainty") or parsed.get("uncertainties"))
    next_steps = _string_list(parsed.get("next_steps") or parsed.get("nextSteps"))
    schema_keys = {"summary", "observations", "uncertainty", "uncertainties",
                   "next_steps", "nextSteps"}
    return {
        "summary": summary,
        "observations": observations,
        "uncertainty": uncertainty,
        "next_steps": next_steps,
        "dropped_evidence": dropped,
        "schema_ok": bool(schema_keys & set(parsed.keys())),
    }


def _named(value: Any) -> str:
    return _clip(value, 80)


# ---------------------------------------------------------------------------
# storage (append-only)
# ---------------------------------------------------------------------------


def _persist(run_id: str, revision: int, result: dict[str, Any]) -> str:
    """Append the attempt to ``ca.llm_reports``; returns a persistence error text."""
    from ..db.connection import tx

    try:
        with tx() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ca.llm_reports (run_id, revision, provider, model,"
                " payload_hash, state, result, error, finished_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, now())",
                (run_id, int(revision), _named(result.get("provider")),
                 _named(result.get("model")), _named(result.get("payload_hash")),
                 _clip(result.get("state") or "ERROR", 20),
                 json.dumps(result, ensure_ascii=False, default=str),
                 _clip(result.get("error"), 1000)),
            )
    except Exception as exc:  # noqa: BLE001 - storage failure must not invent an answer
        return f"{type(exc).__name__}: {exc}"[:1000]
    return ""


def load_report(run_id: str, revision: int | None = None) -> dict[str, Any] | None:
    """Latest stored opinion for a run (read-only); ``None`` if never requested.

    Rows are append-only, so an unsuccessful later attempt would otherwise hide a
    good earlier answer.  The **latest successful** row is returned, and the last
    attempt is attached as ``last_attempt`` whenever it ended badly, so the UI and
    the HTML export can say "this text is from 08:41, the last attempt failed".
    """
    from ..db.connection import query

    select = ("SELECT revision, provider, model, payload_hash, state, result, error,"
              " created_at, finished_at, id FROM ca.llm_reports WHERE run_id = %s")
    scope: list[Any] = [run_id]
    if revision is not None:
        select += " AND revision = %s"
        scope.append(int(revision))

    latest_rows = query(select + " ORDER BY id DESC LIMIT 1", tuple(scope))
    if not latest_rows:
        return None
    latest = latest_rows[0]

    row = latest
    last_attempt: dict[str, Any] | None = None
    if str(latest.get("state") or "") != "DONE":
        done_rows = query(select + " AND state = 'DONE' ORDER BY id DESC LIMIT 1",
                          tuple(scope))
        if done_rows:
            row = done_rows[0]
            last_attempt = {
                "state": str(latest.get("state") or "ERROR"),
                "error": _clip(latest.get("error"), 500),
                "at": latest.get("created_at"),
            }

    stored = row.get("result")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except json.JSONDecodeError:
            stored = {}
    out: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
    out.update({
        "revision": int(row["revision"]),
        "provider": row.get("provider") or out.get("provider") or "",
        "model": row.get("model") or out.get("model") or "",
        "payload_hash": row.get("payload_hash") or out.get("payload_hash") or "",
        "state": row.get("state") or out.get("state") or "ERROR",
        "error": row.get("error") or out.get("error") or "",
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
        "last_attempt": last_attempt,
    })
    out.setdefault("summary", "")
    out.setdefault("observations", [])
    out.setdefault("uncertainty", [])
    out.setdefault("next_steps", [])
    out.setdefault("evidence", [])
    return out


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------


def _error_result(settings, error: str, **extra: Any) -> dict[str, Any]:
    result = {
        "state": "ERROR",
        "provider": getattr(settings, "llm_provider", ""),
        "model": getattr(settings, "llm_model", ""),
        "payload_hash": "",
        "summary": "",
        "observations": [],
        "uncertainty": [],
        "next_steps": [],
        "evidence": [],
        "error": _clip(error, 1000),
    }
    result.update(extra)
    return result


def request_second_opinion(run_id: str, revision: int,
                           payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask the model, validate the answer, store it, and never fabricate one.

    ``payload`` is *extra context* supplied by the caller (for example notes from
    the UI), not a replacement for the packet: it is sanitised, clipped and sent
    as ``extra_context``, so a caller cannot widen what the model sees.

    Returns ``{state, provider, model, payload_hash, payload_chars, latency_ms,
    http_status, usage, response_model, summary, observations[], uncertainty[],
    next_steps[], evidence[], dropped_evidence, trim_notes, error}``.
    """
    settings = load_settings()
    state = status()
    if not state["available"]:
        result = _error_result(settings, state["reason"])
        result.update({"state": "UNAVAILABLE", "reason": state["reason"]})
        result["persist_error"] = _persist(run_id, revision, result)
        return result

    try:
        packet = build_payload(run_id, revision, payload)
        packet, size, dropped_by_size, trim_notes = _trim(
            packet, int(settings.llm_max_payload_chars))
    except Exception as exc:  # noqa: BLE001 - the deterministic report must survive
        result = _error_result(settings, f"{type(exc).__name__}: {exc}")
        result["stage"] = "build_payload"
        result["persist_error"] = _persist(run_id, revision, result)
        return result

    known_ids = {item["evidence_id"] for item in packet.get("evidence") or []}
    payload_hash = hashlib.sha256(
        json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]

    body = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_api_key(settings)}",
        "Content-Type": "application/json",
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"

    base_extra: dict[str, Any] = {
        "payload_hash": payload_hash,
        "payload_chars": size,
        "dropped_by_size": dropped_by_size,
        "trim_notes": trim_notes,
        "evidence": packet.get("evidence") or [],
    }

    started = time.perf_counter()
    try:
        import httpx

        with httpx.Client(timeout=float(settings.llm_timeout_sec)) as client:
            response = client.post(url, headers=headers,
                                   content=json.dumps(body, ensure_ascii=False))
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            snippet = _redact_text(response.text or "")[:400]
            result = _error_result(
                settings,
                f"HTTP {response.status_code} от {url}: {snippet}",
                http_status=response.status_code, latency_ms=latency_ms,
                **base_extra)
            result["persist_error"] = _persist(run_id, revision, result)
            return result
        raw = response.json()
    except Exception as exc:  # noqa: BLE001 - network/timeout/transport JSON
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = _error_result(
            settings, f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms, **base_extra)
        result["persist_error"] = _persist(run_id, revision, result)
        return result

    choices = raw.get("choices") or []
    content = ""
    finish_reason = ""
    if choices and isinstance(choices[0], dict):
        content = ((choices[0].get("message") or {}).get("content") or "")
        finish_reason = _clip(choices[0].get("finish_reason"), 32)
    usage = raw.get("usage") or {}
    response_model = _clip(raw.get("model"), 64)

    parsed = _parse_json_object(content)
    if parsed is None:
        snippet = _clip(_redact_text(content), 400)
        reason = ("ответ модели обрезан по max_tokens (finish_reason=length)"
                  if finish_reason == "length" else
                  "ответ модели не является JSON-объектом")
        result = _error_result(
            settings, reason + (f": {snippet}" if snippet else ""),
            http_status=200, latency_ms=latency_ms, usage=usage,
            response_model=response_model, finish_reason=finish_reason,
            raw_chars=len(content), **base_extra)
        result["persist_error"] = _persist(run_id, revision, result)
        return result

    validated = _validate_response(parsed, known_ids)
    if not validated["schema_ok"]:
        result = _error_result(
            settings,
            "ответ модели не соответствует схеме (нет ни summary, ни observations, "
            "ни uncertainty, ни next_steps)",
            http_status=200, latency_ms=latency_ms, usage=usage,
            response_model=response_model, finish_reason=finish_reason,
            raw_chars=len(content), **base_extra)
        result["persist_error"] = _persist(run_id, revision, result)
        return result

    result = {
        "state": "DONE",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "response_model": response_model,
        "finish_reason": finish_reason,
        "payload_hash": payload_hash,
        "payload_chars": size,
        "dropped_by_size": dropped_by_size,
        "trim_notes": trim_notes,
        "latency_ms": latency_ms,
        "http_status": 200,
        "usage": usage,
        "summary": validated["summary"],
        "observations": validated["observations"],
        "uncertainty": validated["uncertainty"],
        "next_steps": validated["next_steps"],
        "dropped_evidence": validated["dropped_evidence"],
        "evidence": packet.get("evidence") or [],
        "error": "",
    }
    result["persist_error"] = _persist(run_id, revision, result)
    return result
