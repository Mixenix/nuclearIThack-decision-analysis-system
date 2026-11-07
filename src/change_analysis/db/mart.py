"""MartAPI — the only thing a user check may know about storage.

Implements ``checks/README.md`` exactly, over ``ca.comparison_rows`` and
``ca.app_segments``.  Bound to a single ``run_id``; every query carries it.

The delivered checks call this object many times over ~290k rows, so filters are
translated into indexed SQL, never into a full pass in Python.
"""

from __future__ import annotations

from typing import Any, Iterator, Sequence

from ..db.connection import tx

ROW_COLUMNS = (
    "app_id", "prefix", "cls", "suffix", "array_key", "key", "path",
    "value_before", "value_after", "adjusted_before", "adjusted_after",
    "diff_flag", "change_type",
)

SEGMENT_COLUMNS = (
    "app_id", "decision_before", "decision_after", "decision_changed", "products",
    "top_offer_before", "top_offer_after", "partner_type", "routing_method",
    "pd_model_before", "pd_model_after", "cutoff_before", "cutoff_after",
    "reject_reason_before", "reject_reason_after", "score_okb_before", "score_okb_after",
)

_ROW_ALIASES = {
    "APP_ID": "app_id", "PREFIX": "prefix", "CLASS": "cls", "SUFFIX": "suffix",
    "ARRAY_KEY": "array_key", "KEY": "key", "PATH": "path",
    "VALUE_BEFORE": "value_before", "VALUE_AFTER": "value_after",
    "ADJUSTED_BEFORE": "adjusted_before", "ADJUSTED_AFTER": "adjusted_after",
    "DIFF_FLAG": "diff_flag", "CHANGE_TYPE": "change_type",
}

_SEGMENT_ALIASES = {
    "APP_ID": "APP_ID", "DECISION_BEFORE": "DECISION_BEFORE",
    "DECISION_AFTER": "DECISION_AFTER", "DECISION_CHANGED": "DECISION_CHANGED",
    "PRODUCTS": "PRODUCTS", "TOP_OFFER_BEFORE": "TOP_OFFER_BEFORE",
    "TOP_OFFER_AFTER": "TOP_OFFER_AFTER", "PARTNER_TYPE": "PARTNER_TYPE",
    "ROUTING_METHOD": "ROUTING_METHOD", "PD_MODEL_BEFORE": "PD_MODEL_BEFORE",
    "PD_MODEL_AFTER": "PD_MODEL_AFTER", "CUTOFF_BEFORE": "CUTOFF_BEFORE",
    "CUTOFF_AFTER": "CUTOFF_AFTER", "REJECT_REASON_BEFORE": "REJECT_REASON_BEFORE",
    "REJECT_REASON_AFTER": "REJECT_REASON_AFTER",
    "SCORE_OKB_BEFORE": "SCORE_OKB_BEFORE", "SCORE_OKB_AFTER": "SCORE_OKB_AFTER",
}

KNOWN_FILTERS = frozenset(
    {"path", "path_prefix", "key", "array_key", "diff_flag", "change_type",
     "value_before", "value_after", "app_ids"}
)


class MartError(ValueError):
    pass


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    raise MartError(f"filter expects a string or a list of strings, got {type(value).__name__}")


class Mart:
    """Read-only view of one run.  See docs/CONTRACTS.md §2."""

    def __init__(self, run_id: str) -> None:
        self.run_id = str(run_id)

    # -- filter compilation -------------------------------------------------
    def _where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        unknown = set(filters) - KNOWN_FILTERS
        if unknown:
            raise MartError(
                "unknown mart filter: " + ", ".join(sorted(unknown))
                + " (supported: " + ", ".join(sorted(KNOWN_FILTERS)) + ")"
            )
        clauses = ["run_id = %s"]
        params: list[Any] = [self.run_id]

        def add(clause: str, values: Sequence[Any]) -> None:
            clauses.append(clause)
            params.extend(values)

        if "path" in filters and filters["path"] is not None:
            values = _as_list(filters["path"])
            if not values:
                add("false", ())
            elif len(values) == 1:
                add("path = %s", values)
            else:
                add("path = ANY(%s)", (values,))
        if "path_prefix" in filters and filters["path_prefix"] is not None:
            prefix = str(filters["path_prefix"])
            # literal prefix: % and _ are data, not wildcards
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            add("path LIKE %s ESCAPE '\\'", (escaped + "%",))
            clauses[-1] = clauses[-1].replace("path LIKE", "path LIKE")
        if "key" in filters and filters["key"] is not None:
            values = _as_list(filters["key"])
            if not values:
                add("false", ())
            elif len(values) == 1:
                add("key = %s", values)
            else:
                add("key = ANY(%s)", (values,))
        if "array_key" in filters and filters["array_key"] is not None:
            values = _as_list(filters["array_key"])
            if not values:
                add("false", ())
            elif len(values) == 1:
                add("array_key = %s", values)
            else:
                add("array_key = ANY(%s)", (values,))
        if "diff_flag" in filters and filters["diff_flag"] is not None:
            add("diff_flag = %s", (int(filters["diff_flag"]),))
        if "change_type" in filters and filters["change_type"] is not None:
            values = _as_list(filters["change_type"])
            if not values:
                add("false", ())
            elif len(values) == 1:
                add("change_type = %s", values)
            else:
                add("change_type = ANY(%s)", (values,))
        if "value_before" in filters and filters["value_before"] is not None:
            add("value_before = %s", (str(filters["value_before"]),))
        if "value_after" in filters and filters["value_after"] is not None:
            add("value_after = %s", (str(filters["value_after"]),))
        if "app_ids" in filters and filters["app_ids"] is not None:
            values = [str(v) for v in filters["app_ids"]]
            if not values:
                add("false", ())
            else:
                add("app_id = ANY(%s)", (values,))
        return " AND ".join(clauses), params

    # -- public API ---------------------------------------------------------
    def rows(self, **filters: Any) -> Iterator[dict[str, Any]]:
        where, params = self._where(filters)
        sql = (
            "SELECT app_id, prefix, cls, suffix, array_key, key, path,"
            " value_before, value_after, adjusted_before, adjusted_after,"
            " diff_flag, change_type"
            f" FROM ca.comparison_rows WHERE {where}"
            " ORDER BY app_id, path, array_key"
        )
        with tx() as conn, conn.cursor(name="mart_rows") as cur:
            cur.itersize = 2000
            cur.execute(sql, params)
            for record in cur:
                yield {
                    "APP_ID": record["app_id"],
                    "PREFIX": record["prefix"],
                    "CLASS": record["cls"],
                    "SUFFIX": record["suffix"],
                    "ARRAY_KEY": record["array_key"],
                    "KEY": record["key"],
                    "PATH": record["path"],
                    "VALUE_BEFORE": record["value_before"],
                    "VALUE_AFTER": record["value_after"],
                    "ADJUSTED_BEFORE": record["adjusted_before"],
                    "ADJUSTED_AFTER": record["adjusted_after"],
                    "DIFF_FLAG": int(record["diff_flag"]),
                    "CHANGE_TYPE": record["change_type"],
                }

    def count(self, **filters: Any) -> int:
        where, params = self._where(filters)
        with tx() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM ca.comparison_rows WHERE {where}", params)
            return int(cur.fetchone()["n"])

    def app_ids(self, **filters: Any) -> list[str]:
        where, params = self._where(filters)
        with tx() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT app_id FROM ca.comparison_rows"
                f" WHERE {where} ORDER BY app_id", params,
            )
            return [str(row["app_id"]) for row in cur.fetchall()]

    def segments(self) -> Iterator[dict[str, Any]]:
        sql = (
            "SELECT app_id, decision_before, decision_after, decision_changed, products,"
            " top_offer_before, top_offer_after, partner_type, routing_method,"
            " pd_model_before, pd_model_after, cutoff_before, cutoff_after,"
            " reject_reason_before, reject_reason_after, score_okb_before, score_okb_after"
            " FROM ca.app_segments WHERE run_id = %s ORDER BY app_id"
        )
        with tx() as conn, conn.cursor(name="mart_segments") as cur:
            cur.itersize = 2000
            cur.execute(sql, (self.run_id,))
            for record in cur:
                yield {
                    "APP_ID": record["app_id"],
                    "DECISION_BEFORE": record["decision_before"],
                    "DECISION_AFTER": record["decision_after"],
                    "DECISION_CHANGED": int(record["decision_changed"]),
                    "PRODUCTS": record["products"],
                    "TOP_OFFER_BEFORE": record["top_offer_before"],
                    "TOP_OFFER_AFTER": record["top_offer_after"],
                    "PARTNER_TYPE": record["partner_type"],
                    "ROUTING_METHOD": record["routing_method"],
                    "PD_MODEL_BEFORE": record["pd_model_before"],
                    "PD_MODEL_AFTER": record["pd_model_after"],
                    "CUTOFF_BEFORE": record["cutoff_before"],
                    "CUTOFF_AFTER": record["cutoff_after"],
                    "REJECT_REASON_BEFORE": record["reject_reason_before"],
                    "REJECT_REASON_AFTER": record["reject_reason_after"],
                    "SCORE_OKB_BEFORE": record["score_okb_before"],
                    "SCORE_OKB_AFTER": record["score_okb_after"],
                }

    def total_apps(self) -> int:
        with tx() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM ca.app_segments WHERE run_id = %s", (self.run_id,)
            )
            return int(cur.fetchone()["n"])
