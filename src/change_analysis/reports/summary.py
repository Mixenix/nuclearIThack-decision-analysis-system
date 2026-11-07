"""The eight deterministic summary cards of PLAN.md §15.5.

Every card carries the application filter that reproduces it, so the analyst can
click a number and land on exactly those applications.  Nothing here concludes
"safe to release": it reports what was observed and where to look.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..contracts.types import FilterSpec
from ..db.connection import query
from ..services.checks import build_check_report
from ..services.report import _FROM, _predicate, data_revision  # noqa: PLC2701

CAUSE_LABEL = {
    "NONE": "Нет существенных изменений",
    "EXTERNAL_DATA": "Внешние данные",
    "LOGIC": "Логика стратегии",
    "MIXED": "Смешанное (данные и логика)",
    "INSUFFICIENT": "Недостаточно данных",
    "": "Не определено",
}

LIMITATIONS = [
    "Категория источника изменения — наблюдаемый признак, а не доказанная причина: "
    "два среза ответов не дают полного причинного доказательства.",
    "Проверки относятся ко всему прогону и не пересчитываются по фильтрам интерфейса.",
    "APP_IDS в строке проверки — ограниченная выборка, а не полный набор заявок.",
    "Сумма ячеек сводной таблицы может повторно учитывать заявку; все итоги "
    "считаются по уникальным APP_ID.",
]


def build_summary(run_id: str, filters: FilterSpec) -> dict[str, Any]:
    run = query("SELECT revision, data_revision, expected_pairs FROM ca.runs"
                " WHERE id = %s", (run_id,))[0]
    revision = data_revision(run)
    where, params = _predicate(run_id, revision, filters)

    selected_cte = (
        "WITH selected AS MATERIALIZED (SELECT DISTINCT m.app_id" + _FROM
        + " WHERE " + where + ") "
    )
    segments = query(
        selected_cte + "SELECT s.* FROM ca.app_segments s"
        " WHERE s.run_id = %s AND s.app_id IN (SELECT app_id FROM selected)"
        " ORDER BY s.app_id", params + [run_id],
    )
    all_ids = [str(s["app_id"]) for s in segments]
    total = len(all_ids)

    changed = query(
        selected_cte + "SELECT DISTINCT c.app_id FROM ca.comparison_rows c"
        " WHERE c.run_id = %s AND c.diff_flag = 1"
        " AND c.app_id IN (SELECT app_id FROM selected) ORDER BY c.app_id",
        params + [run_id],
    )
    changed_ids = [str(r["app_id"]) for r in changed]

    top = query(
        selected_cte + "SELECT c.path, count(DISTINCT c.app_id) AS cnt,"
        " array_agg(DISTINCT c.app_id ORDER BY c.app_id) AS app_ids"
        " FROM ca.comparison_rows c"
        " WHERE c.run_id = %s AND c.diff_flag = 1"
        " AND c.app_id IN (SELECT app_id FROM selected)"
        " GROUP BY c.path ORDER BY cnt DESC, c.path LIMIT 10",
        params + [run_id],
    )

    base = replace(filters, run_id=str(run_id)).as_dict()

    def card(key: str, label: str, app_ids: list[str], **extra: Any) -> dict[str, Any]:
        return {"id": key, "label": label, "value": len(app_ids),
                "filter": dict(base, app_ids=app_ids), **extra}

    flips = [s for s in segments if int(s["decision_changed"]) == 1]
    flip_ids = [str(s["app_id"]) for s in flips]
    approve_to_reject = [str(s["app_id"]) for s in flips
                         if s["decision_before"] == "APPROVE" and s["decision_after"] != "APPROVE"]
    reject_to_approve = [str(s["app_id"]) for s in flips
                         if s["decision_before"] != "APPROVE" and s["decision_after"] == "APPROVE"]
    other_flips = [i for i in flip_ids if i not in set(approve_to_reject) | set(reject_to_approve)]

    before_approve = [str(s["app_id"]) for s in segments if s["decision_before"] == "APPROVE"]
    after_approve = [str(s["app_id"]) for s in segments if s["decision_after"] == "APPROVE"]
    pct_before = 100.0 * len(before_approve) / total if total else None
    pct_after = 100.0 * len(after_approve) / total if total else None

    checks = build_check_report(run_id, revision, max_revision=revision)
    failures = [r for r in checks["rows"]
                if r["RESULT"] == "FAIL" and r["execution_state"] == "DONE"]

    causes: dict[str, list[str]] = {}
    for segment in segments:
        key = (segment["cause_category"] or "").strip()
        causes.setdefault(key, []).append(str(segment["app_id"]))
    cause_items = [
        card(f"cause_{key or 'unknown'}", CAUSE_LABEL.get(key, key), ids)
        for key, ids in sorted(causes.items(), key=lambda kv: -len(kv[1]))
    ]

    cards: list[dict[str, Any]] = [
        card("total_pairs", "Пар заявок в выборке", all_ids),
        card("changed_apps", "Заявки с существенными различиями", changed_ids),
        card("decision_flips", "Заявки со сменой решения", flip_ids),
        {
            "id": "flip_directions",
            "label": "Направления смены решения",
            "filter": base,
            "items": [
                card("approve_reject", "APPROVE → REJECT", approve_to_reject),
                card("reject_approve", "REJECT → APPROVE", reject_to_approve),
                card("other", "Другие / неизвестные", other_flips),
            ],
        },
        {
            "id": "approval_share",
            "label": "Доля одобрений по заявкам",
            "filter": base,
            "denominator": total,
            "before_pct": pct_before,
            "after_pct": pct_after,
            "delta_pp": None if total == 0 else pct_after - pct_before,
            "before_filter": dict(base, app_ids=before_approve),
            "after_filter": dict(base, app_ids=after_approve),
        },
        {
            "id": "checks",
            "label": "Срабатывания проверок",
            "value": len(failures),
            "high_fail": sum(1 for r in failures if r["CONFIDENCE"] == "HIGH"),
            "low_fail": sum(1 for r in failures if r["CONFIDENCE"] == "LOW"),
            "technical_errors": checks["counts"]["errors"],
            "scope": "Весь прогон",
            "filter": {"run_id": str(run_id)},
            "items": failures,
        },
        {
            "id": "top_attributes",
            "label": "Чаще изменяемые атрибуты (уникальные заявки)",
            "filter": base,
            "items": [
                {"path": r["path"], "cnt": int(r["cnt"]),
                 "filter": dict(base, app_ids=[str(a) for a in (r["app_ids"] or [])])}
                for r in top
            ],
        },
        {
            "id": "causes",
            "label": "Признаки источника изменений",
            "filter": base,
            "items": cause_items,
        },
    ]

    high_fail = sum(1 for r in failures if r["CONFIDENCE"] == "HIGH")
    if total == 0:
        conclusion = "В выборке нет заявок — измените фильтры."
    else:
        conclusion = (
            f"В выборке {total} пар заявок; различия по существу у {len(changed_ids)}, "
            f"смена решения у {len(flip_ids)}. Высокоуверенных срабатываний проверок "
            f"во всём прогоне: {high_fail}."
        )
    return {
        "cards": cards,
        "total_pairs": total,
        "changed_apps": len(changed_ids),
        "decision_flips": len(flip_ids),
        "app_ids": all_ids,
        "empty": total == 0,
        "filters": base,
        "limitations": LIMITATIONS,
        "conclusion": conclusion,
    }
