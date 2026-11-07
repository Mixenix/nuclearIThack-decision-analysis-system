"""MartAPI against PostgreSQL — PLAN.md §21.2 and CONTRACTS.md §2.

Everything here runs read-only against the run produced by the session fixture
(one real 2002-application run) plus small synthetic runs used where the
delivered dataset cannot express a case (a path containing ``%`` or ``_``).
"""

from __future__ import annotations

import pytest

from change_analysis.db.mart import KNOWN_FILTERS, Mart, MartError

from qa_support import REFERENCE, comparison_row, segment_row

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mart(completed_run) -> Mart:
    return Mart(completed_run["run_id"])


# ---------------------------------------------------------------------------
# the numbers that decide whether the data was parsed correctly
# ---------------------------------------------------------------------------


def test_total_apps_is_the_number_of_pairs_not_the_number_of_rows(mart):
    assert mart.total_apps() == REFERENCE["pairs"]
    assert mart.count() == REFERENCE["rows"]
    assert mart.count() > mart.total_apps() * 100, (
        "one application produces hundreds of attribute rows"
    )


def test_reference_application_counts(mart):
    """CONTRACTS.md §0: 502 applications with a difference, 385 decision flips."""
    assert len(mart.app_ids(diff_flag=1)) == REFERENCE["changed"]
    flips = [s for s in mart.segments() if s["DECISION_CHANGED"] == 1]
    assert len(flips) == REFERENCE["decision_changed"]


def test_row_shape_is_exactly_the_contract(mart):
    row = next(mart.rows(path="characteristics.score_okb"))
    assert set(row) == {
        "APP_ID", "PREFIX", "CLASS", "SUFFIX", "ARRAY_KEY", "KEY", "PATH",
        "VALUE_BEFORE", "VALUE_AFTER", "ADJUSTED_BEFORE", "ADJUSTED_AFTER",
        "DIFF_FLAG", "CHANGE_TYPE",
    }
    assert type(row["DIFF_FLAG"]) is int
    assert row["CHANGE_TYPE"] in ("SAME", "VALUE_CHANGED", "ATTRIBUTE_NEW",
                                  "ATTRIBUTE_LOST")


def test_segment_shape_is_exactly_the_contract(mart):
    segment = next(mart.segments())
    assert set(segment) == {
        "APP_ID", "DECISION_BEFORE", "DECISION_AFTER", "DECISION_CHANGED",
        "PRODUCTS", "TOP_OFFER_BEFORE", "TOP_OFFER_AFTER", "PARTNER_TYPE",
        "ROUTING_METHOD", "PD_MODEL_BEFORE", "PD_MODEL_AFTER", "CUTOFF_BEFORE",
        "CUTOFF_AFTER", "REJECT_REASON_BEFORE", "REJECT_REASON_AFTER",
        "SCORE_OKB_BEFORE", "SCORE_OKB_AFTER",
    }
    assert type(segment["DECISION_CHANGED"]) is int
    assert isinstance(segment["PRODUCTS"], str)


def test_absent_attribute_has_no_value(mart):
    """CONTRACTS.md §2: VALUE_BEFORE/AFTER are None where the attribute is absent."""
    new_rows = list(mart.rows(change_type="ATTRIBUTE_NEW",
                              app_ids=mart.app_ids(change_type="ATTRIBUTE_NEW")[:20]))
    lost_rows = list(mart.rows(change_type="ATTRIBUTE_LOST",
                               app_ids=mart.app_ids(change_type="ATTRIBUTE_LOST")[:20]))
    assert new_rows and lost_rows
    assert all(r["VALUE_BEFORE"] is None for r in new_rows)
    assert all(r["VALUE_AFTER"] is None for r in lost_rows)
    assert all(r["VALUE_AFTER"] is not None for r in new_rows)
    assert all(r["VALUE_BEFORE"] is not None for r in lost_rows)


# ---------------------------------------------------------------------------
# filters, one at a time and combined
# ---------------------------------------------------------------------------


def test_count_and_rows_agree(mart):
    for filters in (
        {},
        {"path": "characteristics.score_okb"},
        {"diff_flag": 1},
        {"change_type": ["ATTRIBUTE_NEW", "ATTRIBUTE_LOST"]},
        {"path_prefix": "decisionResult.product_trials.rules."},
    ):
        assert mart.count(**filters) == len(list(mart.rows(**filters))), filters


def test_single_filters(mart):
    assert mart.count(path="characteristics.score_okb") == REFERENCE["pairs"]
    assert mart.count(key="score_okb") >= mart.count(path="characteristics.score_okb")
    assert mart.count(path_prefix="characteristics.") >= REFERENCE["pairs"]
    assert mart.count(diff_flag=0) > mart.count(diff_flag=1) > 0
    assert mart.count(change_type="SAME") == mart.count(diff_flag=0)


def test_array_key_filter(mart):
    sample = next(mart.rows(path="decisionResult.product_trials.rules.hit"))
    assert "|" in sample["ARRAY_KEY"], "the rules array key is product|rule"
    filtered = mart.count(array_key=sample["ARRAY_KEY"])
    assert filtered >= 1
    assert all(r["ARRAY_KEY"] == sample["ARRAY_KEY"]
               for r in mart.rows(array_key=sample["ARRAY_KEY"]))


def test_value_filters(mart):
    bool_rows = mart.count(path="decisionResult.product_trials.rules.hit",
                           value_before="true")
    assert bool_rows > 0
    assert mart.count(path="decisionResult.product_trials.rules.hit",
                      value_before="true", diff_flag=1) <= bool_rows


def test_filters_combine_as_and(mart):
    combined = mart.count(path="characteristics.score_okb", diff_flag=1)
    assert 0 < combined <= mart.count(path="characteristics.score_okb")
    assert combined <= mart.count(diff_flag=1)
    triple = mart.count(path="characteristics.score_okb", diff_flag=1,
                        app_ids=mart.app_ids(diff_flag=1)[:10])
    assert triple <= combined


def test_multi_value_filters_are_a_union(mart):
    new_and_lost = mart.count(change_type=["ATTRIBUTE_NEW", "ATTRIBUTE_LOST"])
    assert new_and_lost == (mart.count(change_type="ATTRIBUTE_NEW")
                            + mart.count(change_type="ATTRIBUTE_LOST"))


def test_app_id_filter_restricts_to_the_given_set(mart):
    sample = mart.app_ids(diff_flag=1)[:5]
    assert mart.app_ids(app_ids=sample) == sorted(sample)


@pytest.mark.parametrize("name", ["path", "key", "array_key", "change_type", "app_ids"])
def test_empty_list_is_an_empty_sample_not_no_filter(mart, name):
    """CONTRACTS.md §2: «app_ids=[] ⇒ empty sample, not „no filter“»."""
    assert mart.count(**{name: []}) == 0
    assert mart.app_ids(**{name: []}) == []


def test_unknown_filter_raises_a_value_error_naming_it(mart):
    with pytest.raises(MartError) as error:
        mart.count(nonsense=1)
    assert "nonsense" in str(error.value)
    assert isinstance(error.value, ValueError)
    assert set(KNOWN_FILTERS) >= {"path", "path_prefix", "key", "array_key",
                                  "diff_flag", "change_type", "value_before",
                                  "value_after", "app_ids"}


# ---------------------------------------------------------------------------
# app_ids
# ---------------------------------------------------------------------------


def test_app_ids_are_unique_sorted_and_inside_the_run(mart):
    ids = mart.app_ids(diff_flag=1)
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert all(isinstance(value, str) for value in ids)
    run_apps = {s["APP_ID"] for s in mart.segments()}
    assert set(ids) <= run_apps


# ---------------------------------------------------------------------------
# literal path prefixes: % and _ are data, not wildcards
# ---------------------------------------------------------------------------


def test_path_prefix_treats_percent_and_underscore_literally(synthetic_run):
    rows = [
        comparison_row("x", "app-1", "a_b.rate", value_before="1", value_after="2",
                       change_type="VALUE_CHANGED"),
        comparison_row("x", "app-1", "aXb.rate", value_before="1", value_after="2",
                       change_type="VALUE_CHANGED"),
        comparison_row("x", "app-1", "100%.rate", value_before="1", value_after="2",
                       change_type="VALUE_CHANGED"),
        comparison_row("x", "app-1", "1000.rate", value_before="1", value_after="2",
                       change_type="VALUE_CHANGED"),
    ]
    run_id = synthetic_run(rows=rows)
    mart = Mart(run_id)

    assert mart.count() == 4
    # an unescaped "_" would also match "aXb.rate"
    assert mart.count(path_prefix="a_b") == 1
    # an unescaped "%" would also match "1000.rate"
    assert mart.count(path_prefix="100%") == 1
    assert mart.count(path_prefix="a") == 2       # a_b.rate + aXb.rate
    assert mart.count(path_prefix="1") == 2       # 100%.rate + 1000.rate
    assert mart.app_ids(path_prefix="100%") == ["app-1"]


def test_path_prefix_does_not_match_mid_path(synthetic_run):
    rows = [comparison_row("x", "app-1", "aa.bb.cc"),
            comparison_row("x", "app-1", "bb.cc")]
    run_id = synthetic_run(rows=rows)
    mart = Mart(run_id)
    assert mart.count(path_prefix="bb") == 1
    assert mart.count(path_prefix="aa.") == 1


# ---------------------------------------------------------------------------
# run binding
# ---------------------------------------------------------------------------


def test_a_mart_is_bound_to_exactly_one_run(synthetic_run, completed_run):
    run_a = synthetic_run(rows=[comparison_row("x", "app-1", "p.q",
                                               change_type="VALUE_CHANGED")],
                          segments=[segment_row("x", "app-1")])
    run_b = synthetic_run(rows=[comparison_row("x", "app-1", "p.q"),
                                comparison_row("x", "app-2", "p.q")],
                          segments=[segment_row("x", "app-1"), segment_row("x", "app-2")])
    assert Mart(run_a).total_apps() == 1
    assert Mart(run_b).total_apps() == 2
    assert Mart(run_a).count() == 1
    assert Mart(completed_run["run_id"]).count() == REFERENCE["rows"]
