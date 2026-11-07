"""Owner-checked analytical services: summary, pivot, drill-down, export.

Every number that claims to count *applications* is a ``COUNT(DISTINCT app_id)``
over the membership of the matching facts.  One application produces hundreds of
attribute rows and appears in both slices, so summing per-fact counts would count
the same person many times (PLAN.md §16.2).  Cells may overlap, and the time the
analyst needs is the number of people, not the number of rows.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import quote, unquote

from ..contracts.types import FilterSpec, PIVOT_DIMENSIONS
from ..db.connection import query
from .errors import AccessDenied, InvalidRequest, NotFound

#: public dimension name -> column of ca.pivot_facts
_COLUMNS = {name: ("cls" if name == "CLASS" else name.lower()) for name in PIVOT_DIMENSIONS}

#: facts joined with their application membership
_FROM = (
    " FROM ca.pivot_facts f"
    " JOIN ca.pivot_membership m"
    "   ON m.fact_id = f.fact_id AND m.run_id = f.run_id AND m.revision = f.revision"
)


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------


def _owned(user_id: str, run_id: str) -> dict[str, Any]:
    rows = query("SELECT * FROM ca.runs WHERE id = %s", (run_id,))
    if not rows:
        raise NotFound("прогон не найден")
    if str(rows[0]["owner_id"]) != str(user_id):
        raise AccessDenied("прогон")
    return rows[0]


def _filters(run_id: str, filters: FilterSpec) -> FilterSpec:
    if not isinstance(filters, FilterSpec):
        raise InvalidRequest("ожидается FilterSpec")
    if filters.run_id and str(filters.run_id) != str(run_id):
        raise InvalidRequest("фильтр относится к другому прогону")
    return replace(filters, run_id=str(run_id))


# ---------------------------------------------------------------------------
# filter compilation — allowlisted dimensions, bound parameters only
# ---------------------------------------------------------------------------


def compile_filter(filters: FilterSpec) -> tuple[str, list[Any]]:
    """SQL predicate over the aliases ``f`` (facts) and ``m`` (membership)."""
    clauses: list[str] = ["f.run_id = %s"]
    params: list[Any] = [filters.run_id]

    if filters.change_types is not None:
        clauses.append("f.change_type = ANY(%s)")
        params.append(list(filters.change_types))
    if filters.paths is not None:
        clauses.append("f.path = ANY(%s)")
        params.append(list(filters.paths))
    if filters.path_prefix is not None:
        prefix = str(filters.path_prefix)
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("f.path LIKE %s ESCAPE '\\'")
        params.append(escaped + "%")
    if filters.source is not None:
        if filters.source not in ("BEFORE", "AFTER"):
            raise InvalidRequest("неизвестный срез")
        clauses.append("f.source = %s")
        params.append(filters.source)
    if filters.diff_flag is not None:
        flag = int(filters.diff_flag)
        if flag not in (0, 1):
            raise InvalidRequest("DIFF_FLAG должен быть 0 или 1")
        clauses.append("f.diff_flag = %s")
        params.append(flag)
    if filters.app_ids is not None:
        clauses.append("m.app_id = ANY(%s)")
        params.append(list(filters.app_ids))
    if filters.decision_changed is not None:
        clauses.append(
            "m.app_id IN (SELECT s.app_id FROM ca.app_segments s"
            " WHERE s.run_id = f.run_id AND s.decision_changed = %s)"
        )
        params.append(1 if filters.decision_changed else 0)
    if filters.products is not None:
        # a set of applications, never a JOIN: a JOIN would multiply rows
        clauses.append(
            "m.app_id IN (SELECT p.app_id FROM ca.product_segments p"
            " WHERE p.run_id = f.run_id AND p.product_id = ANY(%s))"
        )
        params.append(list(filters.products))
    if filters.partners is not None:
        clauses.append(
            "m.app_id IN (SELECT s.app_id FROM ca.app_segments s"
            " WHERE s.run_id = f.run_id AND s.partner_type = ANY(%s))"
        )
        params.append(list(filters.partners))
    if filters.cause_categories is not None:
        clauses.append(
            "m.app_id IN (SELECT s.app_id FROM ca.app_segments s"
            " WHERE s.run_id = f.run_id AND s.cause_category = ANY(%s))"
        )
        params.append(list(filters.cause_categories))
    return " AND ".join(clauses), params


def _predicate(run_id: str, revision: int, filters: FilterSpec) -> tuple[str, list[Any]]:
    sql, params = compile_filter(_filters(run_id, filters))
    return sql + " AND f.revision = %s", params + [revision]


def data_revision(run: dict[str, Any]) -> int:
    """Revision the comparison datamart was written under.

    Separate from ``runs.revision``, which counts published *check reports*: a
    CHECKS_ONLY job bumps that one, and filtering facts by it used to empty the
    pivot table.
    """
    return int(run.get("data_revision") or 1)


# ---------------------------------------------------------------------------
# pivot
# ---------------------------------------------------------------------------


def _dims(dims_rows: Iterable[str], dims_cols: Iterable[str]) -> list[str]:
    dims = list(dims_rows) + list(dims_cols)
    if any(d not in _COLUMNS for d in dims) or len(dims) != len(set(dims)):
        raise InvalidRequest("неизвестные или повторяющиеся измерения")
    return dims


def _combo_key(values: Iterable[Any]) -> str:
    """URL-safe key component; the separator can never appear inside a value."""
    return "|".join(quote(str(v), safe="") for v in values)


def _pivot_sql(dims: list[str], where: str) -> str:
    columns = ", ".join("f." + _COLUMNS[d] for d in dims)
    # GROUPING SETS gives the per-cell counts and the distinct grand total in one
    # round trip; the total is NOT the sum of the cells.
    return (
        f"SELECT {columns}, count(DISTINCT m.app_id) AS cnt,"
        # GROUPING() over several arguments is a bitmask, so "is this the grand
        # total row" is "mask <> 0", not "mask = 1".
        f" GROUPING({columns}) <> 0 AS is_total"
        + _FROM + " WHERE " + where
        + f" GROUP BY GROUPING SETS (({columns}), ())"
    )


def pivot(user_id: str, run_id: str, dims_rows: list[str], dims_cols: list[str],
          filters: FilterSpec, measures: tuple[str, ...] = ("apps",)) -> dict[str, Any]:
    run = _owned(user_id, run_id)
    dims = _dims(dims_rows, dims_cols)
    if tuple(measures) != ("apps",):
        raise InvalidRequest("доступная мера: apps (уникальные заявки)")
    where, params = _predicate(run_id, data_revision(run), filters)
    if not dims:
        # No dimensions: the answer is one number, and it is far cheaper to count
        # distinct applications directly than to join 570k membership rows.
        return {
            "row_dim_values": [], "col_dim_values": [], "cells": {},
            "grand_total": _distinct_count(where, params),
            "row_dims": [], "col_dims": [], "measure_label": "Уникальные заявки",
        }
    rows = query(_pivot_sql(dims, where), params)

    cells: dict[str, dict[str, int]] = {}
    row_values: set[tuple] = set()
    col_values: set[tuple] = set()
    total = 0
    for row in rows:
        if row["is_total"]:
            total = int(row["cnt"])
            continue
        values = tuple(row[_COLUMNS[d]] for d in dims)
        row_combo = values[: len(dims_rows)]
        col_combo = values[len(dims_rows):]
        row_values.add(row_combo)
        col_values.add(col_combo)
        cells[_combo_key(row_combo) + "|" + _combo_key(col_combo)] = {"cnt": int(row["cnt"])}
    return {
        "row_dim_values": [list(v) for v in sorted(row_values, key=lambda t: tuple(str(x) for x in t))],
        "col_dim_values": [list(v) for v in sorted(col_values, key=lambda t: tuple(str(x) for x in t))],
        "cells": cells,
        "grand_total": total,
        "row_dims": list(dims_rows),
        "col_dims": list(dims_cols),
        "measure_label": "Уникальные заявки",
    }


# ---------------------------------------------------------------------------
# pagination helpers
# ---------------------------------------------------------------------------


def _page_bounds(page: int, page_size: int) -> int:
    if not isinstance(page, int) or not isinstance(page_size, int):
        raise InvalidRequest("некорректная страница или размер страницы")
    if page < 1 or not 1 <= page_size <= 1000:
        raise InvalidRequest("некорректная страница или размер страницы")
    return (page - 1) * page_size


def _ids_page(where: str, params: list[Any], page: int, page_size: int) -> dict[str, Any]:
    offset = _page_bounds(page, page_size)
    sql = (
        "WITH ids AS MATERIALIZED ("
        " SELECT DISTINCT m.app_id" + _FROM + " WHERE " + where + "),"
        " paged AS (SELECT app_id FROM ids ORDER BY app_id LIMIT %s OFFSET %s)"
        " SELECT (SELECT count(*) FROM ids) AS total,"
        "        ARRAY(SELECT app_id FROM paged) AS app_ids"
    )
    row = query(sql, params + [page_size, offset])[0]
    return {"app_ids": [str(a) for a in (row["app_ids"] or [])],
            "total": int(row["total"]), "page": page, "page_size": page_size}


def _distinct_count(where: str, params: list[Any]) -> int:
    """COUNT(DISTINCT app_id) for the filtered sample, without the fact join."""
    row = query("SELECT count(DISTINCT m.app_id) AS n" + _FROM + " WHERE " + where,
                params)[0]
    return int(row["n"])


def drill(user_id: str, run_id: str, dims_rows: list[str], dims_cols: list[str],
          filters: FilterSpec, cell_key: str, page: int = 1,
          page_size: int = 50) -> dict[str, Any]:
    """The applications behind one cell — the same predicate, the same distinctness."""
    run = _owned(user_id, run_id)
    dims = _dims(dims_rows, dims_cols)
    if not isinstance(cell_key, str):
        raise InvalidRequest("некорректный ключ ячейки")
    parts = cell_key.split("|")
    if not dims_rows:
        if not parts or parts.pop(0) != "":
            raise InvalidRequest("некорректный ключ строки")
    if not dims_cols:
        if not parts or parts.pop() != "":
            raise InvalidRequest("некорректный ключ столбца")
    if len(parts) != len(dims):
        raise InvalidRequest("некорректный ключ ячейки")
    where, params = _predicate(run_id, data_revision(run), filters)
    for dimension, encoded in zip(dims, parts):
        value: Any = unquote(encoded)
        if dimension == "DIFF_FLAG":
            if value not in ("0", "1"):
                raise InvalidRequest("некорректный DIFF_FLAG")
            value = int(value)
        where += f" AND f.{_COLUMNS[dimension]} = %s"
        params.append(value)
    return _ids_page(where, params, page, page_size)


def apps_page(user_id: str, run_id: str, filters: FilterSpec, page: int = 1,
              page_size: int = 50, sort: str = "APP_ID") -> dict[str, Any]:
    run = _owned(user_id, run_id)
    if sort != "APP_ID":
        raise InvalidRequest("доступная сортировка: APP_ID")
    where, params = _predicate(run_id, data_revision(run), filters)
    result = _ids_page(where, params, page, page_size)
    result["rows"] = query(
        "SELECT * FROM ca.app_segments WHERE run_id = %s AND app_id = ANY(%s)"
        " ORDER BY app_id", (run_id, result["app_ids"]),
    ) if result["app_ids"] else []
    return result


# ---------------------------------------------------------------------------
# application card
# ---------------------------------------------------------------------------


def app_detail(user_id: str, run_id: str, app_id: str) -> dict[str, Any]:
    _owned(user_id, run_id)
    rows = query("SELECT * FROM ca.applications WHERE run_id = %s AND app_id = %s",
                 (run_id, app_id))
    if not rows:
        raise NotFound("заявка не найдена в этом прогоне")
    segments = query(
        "SELECT * FROM ca.app_segments WHERE run_id = %s AND app_id = %s",
        (run_id, app_id),
    )
    products = query(
        "SELECT * FROM ca.product_segments WHERE run_id = %s AND app_id = %s"
        " ORDER BY source, product_id", (run_id, app_id),
    )
    return {**rows[0], "segments": segments, "products": products}


def attribute_rows(user_id: str, run_id: str, app_id: str,
                   include_same: bool = False) -> list[dict[str, Any]]:
    _owned(user_id, run_id)
    sql = ("SELECT * FROM ca.comparison_rows WHERE run_id = %s AND app_id = %s"
           + ("" if include_same else " AND diff_flag = 1")
           + " ORDER BY path, array_key")
    return query(sql, (run_id, app_id))


def raw_json(user_id: str, run_id: str, app_id: str, source: str) -> dict[str, Any]:
    """The original response of one slice, straight from the dataset on disk."""
    import json
    from pathlib import Path

    from ..config import load_settings

    run = _owned(user_id, run_id)
    if source not in ("BEFORE", "AFTER"):
        raise InvalidRequest("неизвестный срез")
    if not query("SELECT 1 FROM ca.applications WHERE run_id = %s AND app_id = %s",
                 (run_id, app_id)):
        raise NotFound("заявка не найдена в этом прогоне")
    # A run carries its own dataset snapshot (services.runs.create_run).  The
    # registry row may be renamed or removed later; the finished report must keep
    # working, so the snapshot wins and the registry is only a fallback.
    snapshot = run.get("snapshot") or {}
    root_ref = (snapshot.get("root_ref") or "").strip()
    slice_dir = ((snapshot.get("slices") or {}).get(source) or {}).get("dir") or ""
    if not root_ref:
        registered = query(
            "SELECT root_ref FROM ca.datasets WHERE id = %s", (run["dataset_id"],))
        if not registered:
            raise NotFound("исходный набор недоступен")
        root_ref = registered[0]["root_ref"]
    root = Path(load_settings().dataset_root).resolve()
    folder = (root / root_ref).resolve()
    if not slice_dir:
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) \
            if manifest_path.is_file() else {}
        slice_dir = manifest.get("srez", {}).get(source, {}).get("dir", source)
    slice_folder = (folder / slice_dir).resolve()
    target = (slice_folder / f"{app_id}.json").resolve()
    # never read outside DATASET_ROOT, whatever the database says
    for candidate, parent in ((folder, root), (slice_folder, folder), (target, slice_folder)):
        if not candidate.is_relative_to(parent):
            raise InvalidRequest("недопустимый путь к исходным данным")
    if not target.is_file():
        raise NotFound("исходный JSON недоступен")
    return json.loads(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# summary and export
# ---------------------------------------------------------------------------


def summary(user_id: str, run_id: str, filters: FilterSpec) -> dict[str, Any]:
    from ..reports.summary import build_summary

    _owned(user_id, run_id)
    result = build_summary(run_id, _filters(run_id, filters))
    align_summary_checks(result, published_check_report(user_id, run_id))
    return result


def published_check_report(user_id: str, run_id: str,
                           revision: int | None = None) -> dict[str, Any]:
    """Select only a complete publication, including technical ERROR outcomes."""
    from .checks import check_report

    run = _owned(user_id, run_id)
    published = query(
        "SELECT revision FROM ca.check_executions WHERE run_id = %s"
        " AND revision <= %s GROUP BY revision"
        " HAVING bool_and(state IN ('DONE', 'ERROR')) ORDER BY revision DESC",
        (run_id, int(run["revision"])),
    )
    revisions = [int(row["revision"]) for row in published]
    # A pipeline configured without checks has a valid empty initial report.
    if not revisions and not (run.get("snapshot") or {}).get("checks"):
        if run["state"] == "SUCCEEDED" and int(run["revision"]) == data_revision(run):
            revisions = [int(run["revision"])]
    rev = revision if revision is not None else (revisions[0] if revisions else None)
    if rev not in revisions:
        raise InvalidRequest("ревизия отчёта ещё не опубликована или неизвестна")
    return check_report(user_id, run_id, rev)


def align_summary_checks(summary: dict[str, Any], checks: dict[str, Any]) -> None:
    """Keep summary counters and narrative on the displayed check revision."""
    counts = checks["counts"]
    for card in summary["cards"]:
        if card["id"] == "checks":
            card.update(value=counts["high_fail"] + counts["low_fail"],
                        high_fail=counts["high_fail"], low_fail=counts["low_fail"],
                        technical_errors=counts["errors"],
                        items=[r for r in checks["rows"] if r["RESULT"] == "FAIL"
                               and r["execution_state"] == "DONE"])
    if not summary.get("empty"):
        summary["conclusion"] = (
            f"В выборке {summary['total_pairs']} пар заявок; различия по существу у "
            f"{summary['changed_apps']}, смена решения у {summary['decision_flips']}. "
            f"Высокоуверенных срабатываний проверок во всём прогоне: {counts['high_fail']}.")


def export_html(user_id: str, run_id: str, revision: int | None = None,
                base_url: str | None = None) -> str:
    from ..config import load_settings
    from ..reports.export import export_report

    run = _owned(user_id, run_id)
    rev = published_check_report(user_id, run_id, revision)["revision"]
    if rev < data_revision(run):
        raise InvalidRequest("витрина этой ревизии больше недоступна")
    settings = load_settings()
    path = settings.artifact_root / str(run["id"]) / f"report-rev{rev}.html"
    # Links inside the document must point where the reader actually is: the
    # configured address is right for a LAN, but wrong when the same page is
    # opened through a tunnel or under another host name.
    resolved = (base_url or "").strip().rstrip("/") or settings.public_base_url
    return export_report(run_id, str(path), resolved)


def available_products(user_id: str, run_id: str, limit: int = 200) -> list[str]:
    _owned(user_id, run_id)
    rows = query(
        "SELECT DISTINCT product_id FROM ca.product_segments WHERE run_id = %s"
        " ORDER BY product_id LIMIT %s", (run_id, limit),
    )
    return [str(r["product_id"]) for r in rows]
