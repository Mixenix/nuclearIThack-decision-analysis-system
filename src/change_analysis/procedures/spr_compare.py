"""Comparison procedure.

Streams the normalized rows written by the normalize stage, groups them per
application, and outer-joins BEFORE against AFTER on ``(PATH, ARRAY_KEY)``.
Every ``SAME`` row is kept: denominators, checks and the "no change" view all
need them.
"""

from __future__ import annotations

from change_analysis.contracts.types import CompareResult
from change_analysis.pipeline.core import compare_app

DEFAULTS = {
    "keep_same": True,
    # Which attribute groups count as "input data" and which as "strategy logic".
    # These are procedure settings, not constants of the kernel (PLAN.md §7.1).
    "external_prefixes": [
        "characteristics.",
        "ext_data.",
        "appData.",
    ],
    "strategy_prefixes": [
        "decisionResult.product_trials.rules.",
        "decisionResult.product_trials.scoring.",
        "decisionResult.product_trials.cutOff.",
        "decisionResult.product_trials.decision",
        "decisionResult.product_trials.partner",
        "decisionResult.product_trials.tariff",
        "decisionResult.product_trials.epr",
        "decisionResult.scoring.",
        "decisionResult.available_offers.",
    ],
    "max_evidence": 5,
}


def _classify(rows, params):
    """Observed signs of where a change came from — never a proven cause.

    Two independent flags plus the outcome, so the analyst sees "the input data
    looked different" separately from "the strategy logic looked different".
    """
    external = [r for r in rows if any(r.path.startswith(p) for p in params["external_prefixes"])]
    strategy = [r for r in rows if any(r.path.startswith(p) for p in params["strategy_prefixes"])]
    if not rows:
        return "NONE", []
    if external and strategy:
        category = "MIXED"
    elif external:
        category = "EXTERNAL_DATA"
    elif strategy:
        category = "LOGIC"
    else:
        category = "INSUFFICIENT"
    evidence = [
        {"path": r.path, "array_key": r.array_key,
         "before": r.value_before, "after": r.value_after,
         "change_type": r.change_type}
        for r in rows[: int(params["max_evidence"])]
    ]
    return category, evidence


def _as_row(raw: dict):
    from change_analysis.pipeline.core import NormalizedRow

    return NormalizedRow(
        app_id=raw["app_id"], source=raw["source"], prefix=raw["prefix"],
        cls=raw["cls"], suffix=raw["suffix"], array_key=raw["array_key"],
        key=raw["key"], path=raw["path"], value=raw["value"],
        value_type=raw["value_type"], adjusted_value=raw["adjusted_value"],
        value_type_raw=raw.get("value_type", ""), pointer=raw.get("pointer", ""),
    )


def run(context, inputs, params):
    """inputs: {"normalized": "<path to normalized_rows.jsonl>"}"""
    params = {**DEFAULTS, **(params or {})}
    keep_same = bool(params.get("keep_same", True))
    normalized_path = inputs["normalized"]
    total_hint = int(inputs.get("app_total") or 0)
    causes: list[dict] = []

    def compared_stream():
        before: list = []
        after: list = []
        current_app = None
        processed = 0

        def flush():
            nonlocal processed
            if current_app is None:
                return
            rows = compare_app(before, after)
            changed = [r for r in rows if r.diff_flag == 1]
            category, evidence = _classify(changed, params)
            causes.append({"app_id": current_app, "category": category,
                           "evidence": evidence, "changed_rows": len(changed)})
            for row in rows:
                if not keep_same and row.change_type == "SAME":
                    continue
                yield {
                    "app_id": row.app_id, "path": row.path,
                    "array_key": row.array_key, "prefix": row.prefix,
                    "cls": row.cls, "suffix": row.suffix, "key": row.key,
                    "value_before": row.value_before, "value_after": row.value_after,
                    "adjusted_before": row.adjusted_before,
                    "adjusted_after": row.adjusted_after,
                    "diff_flag": row.diff_flag, "change_type": row.change_type,
                    "present_before": row.present_before,
                    "present_after": row.present_after,
                    "value_type_before": row.value_type_before,
                    "value_type_after": row.value_type_after,
                }
            processed += 1
            if processed % 25 == 0 or processed == total_hint:
                if causes:
                    context.write_rows("causes", causes)
                    causes.clear()
                context.progress(processed, total_hint, f"compared {processed}/{total_hint}")

        for raw in context.read_rows(normalized_path):
            app_id = raw["app_id"]
            if app_id != current_app:
                if current_app is not None:
                    yield from flush()
                current_app = app_id
                before, after = [], []
            (before if raw["source"] == "BEFORE" else after).append(_as_row(raw))
        if current_app is not None:
            yield from flush()
            if causes:
                context.write_rows("causes", causes)
                causes.clear()

    return CompareResult(rows=compared_stream(), diagnostics=[])


PROCEDURE = {
    "id": "spr_compare_v1",
    "label": "Сравнение срезов BEFORE / AFTER",
    "stage": "compare",
    "contract_version": 1,
    "params": DEFAULTS,
    "entrypoint": run,
}
