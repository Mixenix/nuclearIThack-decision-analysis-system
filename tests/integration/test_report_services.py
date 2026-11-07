"""Summary, pivot and drill-down over the real run — PLAN.md §16, §21.3.

The graded property is exactness: «то, что измерено, — это COUNT(DISTINCT app_id)
по совпадающим фактам, а drill-down возвращает ровно то же множество».
"""

from __future__ import annotations

import json

import pytest

from change_analysis.contracts.types import FilterSpec
from change_analysis.db.mart import Mart
from change_analysis.services import report
from change_analysis.services.errors import InvalidRequest, NotFound

from qa_support import REFERENCE, comparison_row, segment_row

pytestmark = pytest.mark.integration

ALL = FilterSpec()


def filters(run_id: str, **kwargs) -> FilterSpec:
    return FilterSpec(run_id=run_id, **kwargs)


def collect_drill_ids(owner, run_id, dims_rows, dims_cols, spec, cell_key,
                      page_size: int = 1000) -> list[str]:
    """Every application behind a cell, across all pages."""
    collected: list[str] = []
    page = 1
    while True:
        result = report.drill(owner, run_id, dims_rows, dims_cols, spec, cell_key,
                              page=page, page_size=page_size)
        collected.extend(result["app_ids"])
        if len(collected) >= result["total"] or not result["app_ids"]:
            return collected
        page += 1


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_reproduces_the_reference_numbers(completed_run):
    run_id = completed_run["run_id"]
    summary = report.summary(completed_run["owner"], run_id, filters(run_id))

    assert summary["total_pairs"] == REFERENCE["pairs"]
    assert summary["changed_apps"] == REFERENCE["changed"]
    assert summary["decision_flips"] == REFERENCE["decision_changed"]
    assert summary["empty"] is False
    assert len(summary["app_ids"]) == REFERENCE["pairs"]


def test_summary_has_the_eight_cards_and_each_card_can_filter(completed_run):
    run_id = completed_run["run_id"]
    summary = report.summary(completed_run["owner"], run_id, filters(run_id))
    cards = {card["id"]: card for card in summary["cards"]}
    assert len(cards) == 8
    assert set(cards) == {
        "total_pairs", "changed_apps", "decision_flips", "flip_directions",
        "approval_share", "checks", "top_attributes", "causes",
    }
    assert cards["total_pairs"]["value"] == REFERENCE["pairs"]
    assert cards["changed_apps"]["value"] == REFERENCE["changed"]
    assert cards["decision_flips"]["value"] == REFERENCE["decision_changed"]

    approval = cards["approval_share"]
    assert approval["denominator"] == REFERENCE["pairs"]
    assert 0.0 <= approval["before_pct"] <= 100.0
    assert 0.0 <= approval["after_pct"] <= 100.0
    assert approval["delta_pp"] == pytest.approx(approval["after_pct"]
                                                - approval["before_pct"])

    flips = {item["id"]: item["value"] for item in cards["flip_directions"]["items"]}
    assert sum(flips.values()) == REFERENCE["decision_changed"]

    top = cards["top_attributes"]["items"]
    assert top and top[0]["cnt"] >= top[-1]["cnt"]
    assert all(item["filter"]["app_ids"] for item in top)
    # every card carries the filter that reproduces it
    for card in summary["cards"]:
        assert "filter" in card


def test_summary_agrees_with_the_mart(completed_run):
    run_id = completed_run["run_id"]
    mart = Mart(run_id)
    summary = report.summary(completed_run["owner"], run_id, filters(run_id))
    assert summary["changed_apps"] == len(mart.app_ids(diff_flag=1))
    assert summary["decision_flips"] == len([s for s in mart.segments()
                                             if s["DECISION_CHANGED"] == 1])


def test_empty_sample_is_an_empty_state_not_a_crash(completed_run):
    """PLAN.md §21.1: «фильтр даёт пустую выборку → корректное пустое состояние»."""
    run_id = completed_run["run_id"]
    summary = report.summary(completed_run["owner"], run_id,
                             filters(run_id, app_ids=()))
    assert summary["empty"] is True
    assert summary["total_pairs"] == 0
    assert summary["changed_apps"] == 0
    assert summary["decision_flips"] == 0
    approval = {card["id"]: card for card in summary["cards"]}["approval_share"]
    assert approval["before_pct"] is None and approval["after_pct"] is None
    assert approval["delta_pp"] is None
    assert "нет заявок" in summary["conclusion"].lower()


def test_checks_card_does_not_pretend_to_be_recomputed_by_a_filter(completed_run):
    """«Проверки относятся ко всему прогону и не пересчитываются по фильтрам»."""
    run_id = completed_run["run_id"]
    sample = tuple(Mart(run_id).app_ids()[:10])
    whole = report.summary(completed_run["owner"], run_id, filters(run_id))
    narrow = report.summary(completed_run["owner"], run_id,
                            filters(run_id, app_ids=sample))
    whole_checks = {card["id"]: card for card in whole["cards"]}["checks"]
    narrow_checks = {card["id"]: card for card in narrow["cards"]}["checks"]
    assert narrow["total_pairs"] == 10
    assert len(sample) == 10
    assert narrow_checks["value"] == whole_checks["value"]
    assert narrow_checks["scope"] == "Весь прогон"
    assert any("не пересчитыва" in text.lower() for text in whole["limitations"])


# ---------------------------------------------------------------------------
# pivot and drill-down
# ---------------------------------------------------------------------------


def test_pivot_grand_total_is_the_distinct_application_count(completed_run):
    run_id = completed_run["run_id"]
    view = report.pivot(completed_run["owner"], run_id,
                        ["CHANGE_TYPE"], ["SOURCE"], filters(run_id))
    assert view["grand_total"] == REFERENCE["pairs"]
    assert view["row_dim_values"] and view["col_dim_values"]
    assert view["measure_label"] == "Уникальные заявки"


def test_every_pivot_cell_equals_its_drill_down(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    spec = filters(run_id)
    view = report.pivot(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec)

    seen: set[str] = set()
    overlaps = 0
    total_of_cells = 0
    for cell_key, cell in view["cells"].items():
        result = report.drill(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec, cell_key)
        assert result["total"] == cell["cnt"], f"cell {cell_key}"
        ids = collect_drill_ids(owner, run_id, ["CHANGE_TYPE"], ["SOURCE"], spec, cell_key)
        assert len(ids) == cell["cnt"]
        assert ids == sorted(ids)
        overlaps += len(seen & set(ids))
        seen |= set(ids)
        total_of_cells += cell["cnt"]

    assert len(seen) == view["grand_total"], (
        "the cells must cover exactly the applications of the grand total"
    )
    assert total_of_cells >= view["grand_total"]
    if overlaps:
        assert total_of_cells > view["grand_total"], (
            "cells overlap; summing them is not the number of applications"
        )


def test_pivot_without_dimensions_returns_one_number(completed_run):
    run_id = completed_run["run_id"]
    view = report.pivot(completed_run["owner"], run_id, [], [], filters(run_id))
    assert view["cells"] == {}
    assert view["grand_total"] == REFERENCE["pairs"]


def test_pivot_and_drill_agree_on_a_filtered_sample(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    spec = filters(run_id, change_types=("VALUE_CHANGED",))
    view = report.pivot(owner, run_id, ["CLASS"], ["DIFF_FLAG"], spec)
    assert view["grand_total"] <= REFERENCE["pairs"]
    assert view["grand_total"] > 0
    first = next(iter(view["cells"].items()))
    cell_key, cell = first
    ids = collect_drill_ids(owner, run_id, ["CLASS"], ["DIFF_FLAG"], spec, cell_key)
    assert len(ids) == cell["cnt"]


def test_product_filter_does_not_inflate_the_application_set(completed_run):
    """A product filter is a membership test, never a JOIN (PLAN.md §16.2)."""
    run_id = completed_run["run_id"]
    market = Mart(run_id)
    product = sorted({p for s in market.segments()
                      for p in (s["PRODUCTS"] or "").split(";") if p})[0]
    expected = {s["APP_ID"] for s in market.segments()
                if product in (s["PRODUCTS"] or "").split(";")}

    view = report.pivot(completed_run["owner"], run_id, ["CHANGE_TYPE"], [],
                        filters(run_id, products=(product,)))
    assert view["grand_total"] == len(expected)
    assert view["grand_total"] < REFERENCE["pairs"]


def test_pivot_rejects_unknown_and_repeated_dimensions(completed_run):
    run_id = completed_run["run_id"]
    with pytest.raises(InvalidRequest):
        report.pivot(completed_run["owner"], run_id, ["NOPE"], [], filters(run_id))
    with pytest.raises(InvalidRequest):
        report.pivot(completed_run["owner"], run_id, ["CLASS"], ["CLASS"], filters(run_id))
    with pytest.raises(InvalidRequest):
        report.pivot(completed_run["owner"], run_id, ["CLASS"], [], filters(run_id),
                     measures=("rows",))


def test_drill_page_limits(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    spec = filters(run_id, change_types=("SAME",))
    view = report.pivot(owner, run_id, ["SOURCE"], [], spec)
    cell_key = next(iter(view["cells"]))
    page1 = report.drill(owner, run_id, ["SOURCE"], [], spec, cell_key, page=1, page_size=5)
    page2 = report.drill(owner, run_id, ["SOURCE"], [], spec, cell_key, page=2, page_size=5)
    assert page1["total"] == page2["total"] == view["cells"][cell_key]["cnt"]
    assert page1["app_ids"] and page2["app_ids"]
    assert not set(page1["app_ids"]) & set(page2["app_ids"])
    with pytest.raises(InvalidRequest):
        report.drill(owner, run_id, ["SOURCE"], [], spec, cell_key, page=1, page_size=5000)
    with pytest.raises(InvalidRequest):
        report.drill(owner, run_id, ["SOURCE"], [], spec, "not-a-cell-key")


def test_cell_keys_survive_values_with_separators(synthetic_run):
    """``|`` and ``%`` inside a dimension value must not break the cell key."""
    rows = [comparison_row("x", "app-1", "p.A|B.key", value_before="1", value_after="2",
                           change_type="VALUE_CHANGED"),
            comparison_row("x", "app-2", "p.A|B.key", value_before="1", value_after="3",
                           change_type="VALUE_CHANGED"),
            comparison_row("x", "app-3", "p.100%.key", value_before="1", value_after="2",
                           change_type="VALUE_CHANGED")]
    run_id = synthetic_run(rows=rows,
                           segments=[segment_row("x", f"app-{i}") for i in (1, 2, 3)],
                           facts=[{"cls": "A|B", "path": "p.A|B.key", "key": "key",
                                   "change_type": "VALUE_CHANGED", "diff_flag": 1,
                                   "source": "AFTER", "cnt": 2,
                                   "app_ids": ["app-1", "app-2"]},
                                  {"cls": "100%", "path": "p.100%.key", "key": "key",
                                   "change_type": "VALUE_CHANGED", "diff_flag": 1,
                                   "source": "AFTER", "cnt": 1, "app_ids": ["app-3"]}])
    owner = _owner_of(run_id)
    view = report.pivot(owner, run_id, ["CLASS"], [], ALL)
    assert view["grand_total"] == 3
    assert len(view["cells"]) == 2
    for cell_key, cell in view["cells"].items():
        assert "|" in cell_key
        ids = collect_drill_ids(owner, run_id, ["CLASS"], [], ALL, cell_key)
        assert len(ids) == cell["cnt"]


def _owner_of(run_id: str) -> str:
    from change_analysis.db.connection import query

    return str(query("SELECT owner_id FROM ca.runs WHERE id = %s", (run_id,))[0]["owner_id"])


# ---------------------------------------------------------------------------
# application card
# ---------------------------------------------------------------------------


def test_attribute_rows_default_to_changes_only(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    mart = Mart(run_id)
    app_id = mart.app_ids(diff_flag=1)[0]

    changed = report.attribute_rows(owner, run_id, app_id)
    everything = report.attribute_rows(owner, run_id, app_id, include_same=True)
    assert changed
    assert all(row["diff_flag"] == 1 for row in changed)
    assert len(everything) > len(changed)
    assert {row["path"] for row in changed} <= {row["path"] for row in everything}


def test_app_detail_returns_both_layers(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    app_id = Mart(run_id).app_ids(diff_flag=1)[0]
    detail = report.app_detail(owner, run_id, app_id)
    assert detail["app_id"] == app_id
    assert len(detail["segments"]) == 1
    assert isinstance(detail["products"], list)
    with pytest.raises(NotFound):
        report.app_detail(owner, run_id, "no-such-application")


def test_raw_json_is_the_file_from_disk(completed_run):
    run_id = completed_run["run_id"]
    owner = completed_run["owner"]
    app_id = Mart(run_id).app_ids()[0]
    payload = report.raw_json(owner, run_id, app_id, "BEFORE")
    assert payload["appData"]["application_id"] == app_id
    assert json.dumps(payload)  # serialisable
    with pytest.raises(InvalidRequest):
        report.raw_json(owner, run_id, app_id, "SIDEWAYS")


def test_available_products_are_sorted_and_bounded(completed_run):
    products = report.available_products(completed_run["owner"], completed_run["run_id"],
                                         limit=10)
    assert 0 < len(products) <= 10
    assert products == sorted(products)
