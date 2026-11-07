"""Aggregation procedure — turns comparison rows into pivot facts.

A fact is one combination of the report dimensions; its ``CNT`` is the number of
**distinct applications** in it, never the number of attribute rows.  The
``SOURCE`` dimension splits each comparison row into the slices in which the
attribute actually exists, so ``ATTRIBUTE_NEW`` only contributes an AFTER fact
and ``ATTRIBUTE_LOST`` only a BEFORE fact (PLAN.md §9.3).
"""

from __future__ import annotations

from change_analysis.contracts.types import AggregateResult

DEFAULTS = {
    "dimensions": ["PREFIX", "CLASS", "SUFFIX", "ARRAY_KEY", "KEY", "PATH",
                   "VALUE_ADJUSTED", "DIFF_FLAG", "CHANGE_TYPE", "SOURCE"],
}


def _dimension_values(row: dict) -> dict[str, str]:
    return {
        "PREFIX": row["prefix"],
        "CLASS": row["cls"],
        "SUFFIX": row["suffix"],
        "ARRAY_KEY": row["array_key"],
        "KEY": row["key"],
        "PATH": row["path"],
        "VALUE_ADJUSTED": "",       # filled per slice below
        "DIFF_FLAG": "1" if row["diff_flag"] else "0",
        "CHANGE_TYPE": row["change_type"],
        "SOURCE": "",
    }


def run(context, inputs, params):
    """inputs: {"comparison": "<path to comparison_rows.jsonl>"}"""
    params = {**DEFAULTS, **(params or {})}
    dims = tuple(params.get("dimensions") or DEFAULTS["dimensions"])
    path = inputs["comparison"]
    total_hint = int(inputs.get("expected_rows") or 0)

    order: list[tuple] = []
    buckets: dict[tuple, set[str]] = {}
    seen_rows = 0

    for row in context.read_rows(path):
        base = _dimension_values(row)
        sides = []
        if row["present_before"]:
            sides.append(("BEFORE", row["adjusted_before"]))
        if row["present_after"]:
            sides.append(("AFTER", row["adjusted_after"]))
        for source, adjusted in sides:
            values = dict(base)
            values["VALUE_ADJUSTED"] = "" if adjusted is None else adjusted
            values["SOURCE"] = source
            key = tuple(values[d] for d in dims)
            app_set = buckets.get(key)
            if app_set is None:
                buckets[key] = app_set = set()
                order.append(key)
            app_set.add(row["app_id"])
        seen_rows += 1
        if seen_rows % 5000 == 0:
            context.progress(seen_rows, total_hint, f"aggregated {seen_rows} rows")

    def facts_stream():
        index = 0
        for key in order:
            app_set = buckets[key]
            values = dict(zip(dims, key))
            fact = {
                "fact_index": index,
                "prefix": values.get("PREFIX", ""),
                "cls": values.get("CLASS", ""),
                "suffix": values.get("SUFFIX", ""),
                "array_key": values.get("ARRAY_KEY", ""),
                "key": values.get("KEY", ""),
                "path": values.get("PATH", ""),
                "value_adjusted": values.get("VALUE_ADJUSTED", ""),
                "diff_flag": 1 if values.get("DIFF_FLAG") == "1" else 0,
                "change_type": values.get("CHANGE_TYPE", ""),
                "source": values.get("SOURCE", ""),
                "cnt": len(app_set),
            }
            for app_id in sorted(app_set):
                membership.append((index, app_id))
            index += 1
            yield fact

    membership: list[tuple[int, str]] = []
    facts = list(facts_stream())
    context.set_meta("facts", len(facts))
    context.set_meta("membership", len(membership))
    context.progress(seen_rows, seen_rows, f"{len(facts)} facts")

    # membership is written by the runner through this side table
    context.write_rows("membership", ({"fact_index": i, "app_id": a} for i, a in membership))
    return AggregateResult(facts=facts, membership=[], summary={"facts": len(facts)},
                           diagnostics=[])


PROCEDURE = {
    "id": "spr_aggregate_v1",
    "label": "Агрегация фактов для сводной таблицы",
    "stage": "aggregate",
    "contract_version": 1,
    "params": DEFAULTS,
    "entrypoint": run,
}
