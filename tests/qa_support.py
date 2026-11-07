"""Infrastructure for the QA acceptance suite (no product code lives here).

Three things are defined here:

``assert_test_database``
    the safety interlock.  Anything that deletes rows calls it first, so a
    mis-set ``DATABASE_URL`` cannot wipe the shared demo database.

``StubMart``
    an in-memory implementation of the MartAPI of ``checks/README.md``.  It is
    used only by *unit* tests that need a Mart with a hand-built shape (e.g. "no
    application was approved anywhere" for the division-by-zero cases) without
    paying for a 2002-application pipeline run.  The real ``ca.db.mart.Mart`` is
    exercised by the integration tests — a stub is never accepted as proof that
    SQL filtering works.

``insert_comparison_rows`` / ``insert_segments`` / ``insert_facts``
    helpers that seed small, *explicitly synthetic* runs (used for filter
    escaping and for the checks-only contract), so that those tests never touch
    the expensive full run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

# ---------------------------------------------------------------------------
# safety interlock
# ---------------------------------------------------------------------------

#: the only database this suite is allowed to write to / delete from
TEST_DB_MARKER = "change_analysis_test"


def assert_test_database(dsn: str) -> None:
    """Refuse any destructive helper when the DSN is not the test database."""
    if TEST_DB_MARKER not in str(dsn):
        raise RuntimeError(
            "refusing a destructive operation: DATABASE_URL is "
            f"{dsn!r}, which is not the test database ({TEST_DB_MARKER}). "
            "Start the suite with TEST_DATABASE_URL pointing at a throw-away "
            "database; the shared demo database is read-only for this suite."
        )


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# path splitting used to seed synthetic rows (deliberately independent of
# change_analysis.pipeline.core: a fixture must not inherit the bug it may hide)
# ---------------------------------------------------------------------------


def split_path(path: str) -> tuple[str, str, str, str]:
    """``PREFIX, CLASS, SUFFIX, KEY`` — mirrors СХЕМА_ВИТРИНЫ §1."""
    segments = path.split(".")
    key = segments[-1]
    prefix = segments[0]
    cls = segments[1] if len(segments) > 2 else ""
    suffix = ".".join(segments[2:-1]) if len(segments) > 2 else ""
    return prefix, cls, suffix, key


# ---------------------------------------------------------------------------
# StubMart — the checks/README.md contract, in memory
# ---------------------------------------------------------------------------

_KNOWN_FILTERS = frozenset(
    {"path", "path_prefix", "key", "array_key", "diff_flag", "change_type",
     "value_before", "value_after", "app_ids"}
)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


#: database column -> check-facing column, exactly as ``Mart.segments()`` maps it
SEGMENT_UPPERCASE = {
    "app_id": "APP_ID", "decision_before": "DECISION_BEFORE",
    "decision_after": "DECISION_AFTER", "decision_changed": "DECISION_CHANGED",
    "products": "PRODUCTS", "top_offer_before": "TOP_OFFER_BEFORE",
    "top_offer_after": "TOP_OFFER_AFTER", "partner_type": "PARTNER_TYPE",
    "routing_method": "ROUTING_METHOD", "pd_model_before": "PD_MODEL_BEFORE",
    "pd_model_after": "PD_MODEL_AFTER", "cutoff_before": "CUTOFF_BEFORE",
    "cutoff_after": "CUTOFF_AFTER", "reject_reason_before": "REJECT_REASON_BEFORE",
    "reject_reason_after": "REJECT_REASON_AFTER",
    "score_okb_before": "SCORE_OKB_BEFORE", "score_okb_after": "SCORE_OKB_AFTER",
}


def _as_segment_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Accept both a seeded (database-shaped) row and a check-facing row."""
    if "APP_ID" in row:
        return dict(row)
    return {SEGMENT_UPPERCASE[k]: v for k, v in row.items() if k in SEGMENT_UPPERCASE}


class StubMart:
    """A truthful, tiny MartAPI: same filters, same return shapes."""

    def __init__(self, rows: Iterable[Mapping[str, Any]] = (),
                 segments: Iterable[Mapping[str, Any]] = ()) -> None:
        self._rows = [dict(r) for r in rows]
        self._segments = [_as_segment_row(s) for s in segments]

    # -- filter compilation -------------------------------------------------
    def _matches(self, row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        unknown = set(filters) - _KNOWN_FILTERS
        if unknown:
            raise ValueError("unknown mart filter: " + ", ".join(sorted(unknown)))
        for name, value in filters.items():
            if value is None:
                continue
            if name == "path_prefix":
                if not str(row["PATH"]).startswith(str(value)):
                    return False
            elif name in ("path", "key", "array_key", "change_type"):
                allowed = _as_list(value)
                if not allowed or str(row[name.upper()]) not in allowed:
                    return False
            elif name == "app_ids":
                allowed = [str(v) for v in value]
                if not allowed or str(row["APP_ID"]) not in allowed:
                    return False
            elif name == "diff_flag":
                if int(row["DIFF_FLAG"]) != int(value):
                    return False
            elif name in ("value_before", "value_after"):
                column = "VALUE_BEFORE" if name == "value_before" else "VALUE_AFTER"
                if row.get(column) != str(value):
                    return False
            else:  # pragma: no cover - all names are covered above
                raise AssertionError(name)
        return True

    # -- public API ---------------------------------------------------------
    def rows(self, **filters: Any) -> Iterator[dict[str, Any]]:
        for row in self._rows:
            if self._matches(row, filters):
                yield dict(row)

    def count(self, **filters: Any) -> int:
        return sum(1 for _ in self.rows(**filters))

    def app_ids(self, **filters: Any) -> list[str]:
        return sorted({str(r["APP_ID"]) for r in self.rows(**filters)})

    def segments(self) -> Iterator[dict[str, Any]]:
        for row in self._segments:
            yield dict(row)

    def total_apps(self) -> int:
        return len({str(s["APP_ID"]) for s in self._segments})


# ---------------------------------------------------------------------------
# synthetic seeding into the test database
# ---------------------------------------------------------------------------

COMPARISON_COLUMNS = (
    "run_id", "app_id", "path", "array_key", "prefix", "cls", "suffix", "key",
    "value_before", "value_after", "adjusted_before", "adjusted_after",
    "diff_flag", "change_type", "present_before", "present_after",
    "value_type_before", "value_type_after",
)


def comparison_row(run_id: str, app_id: str, path: str, array_key: str = "",
                   value_before: str | None = "", value_after: str | None = "",
                   change_type: str = "SAME", diff_flag: int | None = None,
                   value_type: str = "string") -> dict[str, Any]:
    prefix, cls, suffix, key = split_path(path)
    if diff_flag is None:
        diff_flag = 0 if change_type == "SAME" else 1
    return {
        "run_id": run_id, "app_id": app_id, "path": path, "array_key": array_key,
        "prefix": prefix, "cls": cls, "suffix": suffix, "key": key,
        "value_before": value_before, "value_after": value_after,
        "adjusted_before": value_before, "adjusted_after": value_after,
        "diff_flag": diff_flag, "change_type": change_type,
        "present_before": change_type != "ATTRIBUTE_NEW",
        "present_after": change_type != "ATTRIBUTE_LOST",
        "value_type_before": None if change_type == "ATTRIBUTE_NEW" else value_type,
        "value_type_after": None if change_type == "ATTRIBUTE_LOST" else value_type,
    }


def insert_comparison_rows(execute, rows: Iterable[Mapping[str, Any]]) -> int:
    """``execute`` is ``change_analysis.db.connection.execute``."""
    sql = (
        "INSERT INTO ca.comparison_rows (" + ", ".join(COMPARISON_COLUMNS) + ")"
        " VALUES (" + ", ".join(["%s"] * len(COMPARISON_COLUMNS)) + ")"
    )
    count = 0
    for row in rows:
        execute(sql, tuple(row[c] for c in COMPARISON_COLUMNS))
        count += 1
    return count


def insert_segments(execute, rows: Iterable[Mapping[str, Any]]) -> int:
    columns = (
        "run_id", "app_id", "decision_before", "decision_after", "decision_changed",
        "products", "top_offer_before", "top_offer_after", "partner_type",
        "routing_method", "pd_model_before", "pd_model_after", "cutoff_before",
        "cutoff_after", "reject_reason_before", "reject_reason_after",
        "score_okb_before", "score_okb_after", "reference_product_id",
    )
    sql = ("INSERT INTO ca.app_segments (" + ", ".join(columns) + ") VALUES ("
           + ", ".join(["%s"] * len(columns)) + ")")
    count = 0
    for row in rows:
        execute(sql, tuple(row[c] for c in columns))
        count += 1
    return count


def segment_row(run_id: str, app_id: str, decision_before: str = "APPROVE",
                decision_after: str = "APPROVE", products: str = "15210",
                pd_model_before: str = "MODEL_A", pd_model_after: str = "MODEL_A",
                cutoff_before: str = "CUT_A", cutoff_after: str = "CUT_A",
                top_offer_before: str = "15210", top_offer_after: str = "15210",
                partner_type: str = "BANK", routing_method: str = "SCORE",
                reject_reason_before: str = "", reject_reason_after: str = "",
                score_okb_before: str = "700", score_okb_after: str = "700",
                reference_product_id: str = "15210") -> dict[str, Any]:
    return {
        "run_id": run_id, "app_id": app_id,
        "decision_before": decision_before, "decision_after": decision_after,
        "decision_changed": 0 if decision_before == decision_after else 1,
        "products": products, "top_offer_before": top_offer_before,
        "top_offer_after": top_offer_after, "partner_type": partner_type,
        "routing_method": routing_method, "pd_model_before": pd_model_before,
        "pd_model_after": pd_model_after, "cutoff_before": cutoff_before,
        "cutoff_after": cutoff_after, "reject_reason_before": reject_reason_before,
        "reject_reason_after": reject_reason_after,
        "score_okb_before": score_okb_before, "score_okb_after": score_okb_after,
        "reference_product_id": reference_product_id,
    }


def insert_facts(tx, run_id: str, revision: int,
                 facts: Iterable[Mapping[str, Any]]) -> list[int]:
    """Insert facts and their membership; returns the new ``fact_id`` values."""
    fact_ids: list[int] = []
    with tx() as conn, conn.cursor() as cur:
        for index, fact in enumerate(facts):
            cur.execute(
                "INSERT INTO ca.pivot_facts (run_id, revision, fact_index, prefix, cls,"
                " suffix, array_key, key, path, value_adjusted, diff_flag, change_type,"
                " source, cnt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING fact_id",
                (run_id, revision, index, fact.get("prefix", ""), fact.get("cls", ""),
                 fact.get("suffix", ""), fact.get("array_key", ""),
                 fact.get("key", ""), fact.get("path", ""),
                 fact.get("value_adjusted", ""), int(fact.get("diff_flag", 0)),
                 fact.get("change_type", ""), fact.get("source", ""),
                 int(fact.get("cnt", 0))),
            )
            fact_id = int(cur.fetchone()["fact_id"])
            fact_ids.append(fact_id)
            for app_id in fact.get("app_ids", ()):
                cur.execute(
                    "INSERT INTO ca.pivot_membership (run_id, revision, fact_id, app_id)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (run_id, revision, fact_id, str(app_id)),
                )
    return fact_ids


def insert_check_result(tx, run_id: str, revision: int, check_id: str, label: str,
                        *, state: str = "DONE", rows: Iterable[Mapping[str, Any]] = ()
                        ) -> int:
    """Publish a check execution with its rows, as the worker does."""
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.check_executions (run_id, revision, check_id, label,"
            " source_kind, source_ref, params, state) VALUES (%s,%s,%s,%s,'workspace',"
            " '', '{}'::jsonb, %s) RETURNING id",
            (run_id, revision, check_id, label, state),
        )
        execution_id = int(cur.fetchone()["id"])
        for row in rows:
            cur.execute(
                "INSERT INTO ca.check_results (execution_id, run_id, revision, label,"
                " source, argument, cnt, total, metric, app_ids, result, confidence)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (execution_id, run_id, revision, row["LABEL"], row["SOURCE"],
                 row.get("ARGUMENT", ""), int(row["CNT"]), int(row["TOTAL"]),
                 row.get("METRIC"), list(row.get("APP_IDS") or []), row["RESULT"],
                 row["CONFIDENCE"]),
            )
    return execution_id


# ---------------------------------------------------------------------------
# procedure execution context used by the unit tests
# ---------------------------------------------------------------------------


class FakeContext:
    """The ``context`` object promised by procedures/README.md §контракт."""

    def __init__(self, artifact_dir: str | Path = ".",
                 rows: Iterable[Mapping[str, Any]] = ()) -> None:
        self.run_id = "qa-unit-run"
        self.backend = "cpu"
        self.artifact_dir = Path(artifact_dir)
        self.logs: list[str] = []
        self.progress_calls: list[tuple[int, int, str]] = []
        self.meta: dict[str, Any] = {}
        self.written: dict[str, list[dict[str, Any]]] = {}
        self._rows = [dict(r) for r in rows]

    def log(self, message: str) -> None:
        self.logs.append(str(message))

    def progress(self, done: int, total: int, message: str = "") -> None:
        self.progress_calls.append((int(done), int(total), message))

    def read_rows(self, path: str | Path) -> Iterator[dict[str, Any]]:
        source = Path(path)
        if source.exists():
            with source.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)
            return
        for row in self._rows:
            yield dict(row)

    def write_rows(self, name: str, rows: Iterable[Mapping[str, Any]]) -> int:
        bucket = self.written.setdefault(name, [])
        count = 0
        for row in rows:
            bucket.append(dict(row))
            count += 1
        return count

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value


# ---------------------------------------------------------------------------
# reference numbers (the organizers' own counts, CONTRACTS.md §0)
# ---------------------------------------------------------------------------

REFERENCE = {
    "pairs": 2002,
    "rows": 289_533,
    "changed": 502,
    "decision_changed": 385,
    "holdout_pairs": 998,
    "holdout_changed": 248,
    "holdout_decision_changed": 186,
}
