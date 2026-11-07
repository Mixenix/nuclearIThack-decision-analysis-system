"""Deterministic HTML export.

The document must be readable with no network and no application running, so all
styling is inlined and every number is already computed.  Links point back into
the live application and therefore require being logged in — the document says so
explicitly instead of pretending the links are public.

The report also carries the optional block "Независимое мнение ИИ": when a stored
opinion exists it is rendered as *text of a model*, clearly separated from the
deterministic figures, and each observation shows the evidence row it cited (an
``evidence_id`` that was not in the packet was already dropped by the adapter).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from jinja2 import Environment, FileSystemLoader
from markupsafe import escape

from ..contracts.types import FilterSpec
from ..db.connection import query
from ..llm.adapter import config_digest, load_report
from ..services.checks import build_check_report
from ..services.errors import InvalidRequest, NotFound
from .summary import CAUSE_LABEL, build_summary

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"

# Application identifiers are ``I32554362``-shaped; anything else is rendered as
# plain text so a hostile value can never become part of an href.
_APP_ID_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")

LLM_DISCLAIMER = (
    "Это мнение языковой модели по ограниченному пакету данных, а не проверка. "
    "Оно не меняет ни одного числа, вердикта или confidence в детерминированной "
    "части отчёта: все CNT/TOTAL, PASS/FAIL и категории посчитаны приложением."
)


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,                     # every variable is escaped by default
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["json_text"] = lambda value: escape(
        json.dumps(value, ensure_ascii=False, default=str, indent=2)
    )
    env.filters["json_inline"] = lambda value: _inline_json(value)
    env.filters["urlpart"] = lambda value: quote(str(value), safe="")
    env.filters["cause"] = lambda value: CAUSE_LABEL.get(str(value or ""), str(value or ""))
    env.filters["safe_app_id"] = is_safe_app_id
    return env


def _inline_json(value: object, limit: int = 240) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_safe_app_id(value: object) -> bool:
    """True for ``I32554362``-shaped identifiers; used before building an href."""
    return bool(_APP_ID_RE.fullmatch(str(value or "")))


def _format_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _time_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return str(value)[:19].replace("T", " ")


def _llm_view(stored: dict | None) -> dict | None:
    """Shape the stored opinion for the template; never invents content."""
    if not stored:
        return None
    evidence_by_id = {
        item.get("evidence_id"): item
        for item in (stored.get("evidence") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    observations = []
    for item in stored.get("observations") or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        observations.append({
            "text": str(item.get("text") or ""),
            "evidence_id": evidence_id,
            "evidence": evidence_by_id.get(evidence_id),
        })
    usage = stored.get("usage") if isinstance(stored.get("usage"), dict) else {}
    last_attempt = stored.get("last_attempt") if isinstance(
        stored.get("last_attempt"), dict) else None
    if last_attempt:
        last_attempt = {
            "state": str(last_attempt.get("state") or "ERROR"),
            "error": str(last_attempt.get("error") or ""),
            "at": _time_text(last_attempt.get("at")),
        }
    return {
        "state": str(stored.get("state") or "ERROR"),
        "provider": str(stored.get("provider") or ""),
        "model": str(stored.get("model") or ""),
        "response_model": str(stored.get("response_model") or ""),
        "payload_hash": str(stored.get("payload_hash") or ""),
        "latency_ms": stored.get("latency_ms"),
        "requested_at": _time_text(stored.get("created_at")),
        "usage": usage,
        "summary": str(stored.get("summary") or ""),
        "observations": observations,
        "uncertainty": [str(u) for u in (stored.get("uncertainty") or [])],
        "next_steps": [str(s) for s in (stored.get("next_steps") or [])],
        "dropped_evidence": int(stored.get("dropped_evidence") or 0),
        "error": str(stored.get("error") or ""),
        "last_attempt": last_attempt,
        "disclaimer": LLM_DISCLAIMER,
    }


def _llm_status() -> dict | None:
    from ..llm.adapter import status as llm_status

    try:
        return llm_status()
    except Exception:  # noqa: BLE001 - the report must render without the LLM block
        return None


def render_report(run: dict, summary: dict, checks: dict, base_url: str,
                  generated_at: datetime | None = None,
                  llm: dict | None = None,
                  llm_status: dict | None = None,
                  configuration: dict | None = None) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidRequest("PUBLIC_BASE_URL должен быть абсолютным HTTP(S) URL")
    base = base_url.rstrip("/")
    run_id = quote(str(run["id"]), safe="")
    generated = generated_at or datetime.now(timezone.utc)
    template = _environment().get_template("report.html.j2")
    return template.render(
        run=run,
        summary=summary,
        checks=checks,
        base_url=base,
        run_url=f"{base}/runs/{run_id}",
        report_url=f"{base}/runs/{run_id}/report",
        explore_url=f"{base}/runs/{run_id}/explore",
        run_created_text=_time_text(run.get("created_at")),
        generated_at=generated.strftime("%Y-%m-%d %H:%M:%S UTC"),
        fmt_pct=_format_number,
        llm=llm,
        llm_status=llm_status,
        configuration=configuration,
    )


def export_report(run_id: str, out_path: str, base_url: str) -> str:
    path = Path(out_path).resolve()
    match = re.fullmatch(r"report-rev([1-9][0-9]*)\.html", path.name)
    rows = query("SELECT * FROM ca.runs WHERE id = %s", (run_id,))
    if not rows:
        raise NotFound("прогон не найден")
    run = dict(rows[0])
    from ..services.report import align_summary_checks, data_revision, published_check_report

    requested = int(match.group(1)) if match else None
    checks = published_check_report(str(run["owner_id"]), run_id, requested)
    revision = checks["revision"]
    if revision < data_revision(run):
        raise InvalidRequest("витрина этой ревизии больше недоступна")

    snapshot = run.get("snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            snapshot = {}
    configuration = config_digest(snapshot) if isinstance(snapshot, dict) else None

    dataset = query("SELECT name, root_ref FROM ca.datasets WHERE id = %s",
                    (run["dataset_id"],)) if run.get("dataset_id") else []
    run["dataset_name"] = dataset[0]["name"] if dataset else ""
    run["dataset_root"] = dataset[0]["root_ref"] if dataset else ""

    summary = build_summary(run_id, FilterSpec(run_id=str(run_id)))
    align_summary_checks(summary, checks)
    llm = _llm_view(load_report(run_id, revision))
    html = render_report(run, summary, checks, base_url, llm=llm,
                         llm_status=_llm_status(), configuration=configuration)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(path)          # atomic publish: no half-written report
    return str(path)
