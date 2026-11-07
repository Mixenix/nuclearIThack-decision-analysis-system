"""The slice layer (СХЕМА_ВИТРИНЫ §2) — "у кого изменилось".

The delivered checks read this layer directly:
``check_ar_drop_by_product`` splits ``PRODUCTS`` on ``";"``,
``check_target_model_changed`` compares ``PD_MODEL_BEFORE``/``PD_MODEL_AFTER`` and
``check_stop_factor_disappeared`` relies on ``DECISION_CHANGED`` being a number.
So the column types and the documented ambiguity rules are graded.
"""

from __future__ import annotations

import pytest

from change_analysis.pipeline.segments import build_product_segments, build_segment

pytestmark = pytest.mark.unit


def document(*, decision="APPROVE", offers=(), trials=(), score_okb=None):
    payload = {"decisionResult": {"decision": decision,
                                  "available_offers": list(offers),
                                  "product_trials": list(trials)}}
    if score_okb is not None:
        payload["characteristics"] = {"score_okb": score_okb}
    return payload


def trial(product_id, *, decision="APPROVE", pd_model="MODEL_A", cutoff="CUT_A",
          reject="", rank=None):
    return {
        "product_id": product_id,
        "decision": decision,
        "partner_type": "BANK",
        "routing_method": "SCORE",
        "rejectReason": reject,
        "scoring": {"pd": {"modelName": pd_model}},
        "cutOff": {"name": cutoff, "value": 0.5},
    }


def offer(product_id, kind="MICRO", rank=1):
    return {"product_id": product_id, "type": kind, "rank": rank}


def test_decision_changed_is_a_real_int():
    segment = build_segment("app-1", document(), document(decision="REJECT"))
    assert type(segment["DECISION_CHANGED"]) is int
    assert segment["DECISION_CHANGED"] == 1

    same = build_segment("app-1", document(), document())
    assert type(same["DECISION_CHANGED"]) is int
    assert same["DECISION_CHANGED"] == 0


def test_products_is_a_semicolon_joined_sorted_union():
    """The delivered checks split this value on ``";"``, so the separator and the
    absence of duplicates are what matter.  The *order* is lexicographic today:
    ``_sort_key`` has a numeric branch, but it is fed strings that were already
    converted with ``str()``, so that branch never fires (see the QA report —
    cosmetic, the reference comparison passes)."""
    before = document(trials=[trial(15210), trial(999)])
    after = document(trials=[trial(15210), trial(30000)])
    segment = build_segment("app-1", before, after)
    assert segment["PRODUCTS"].split(";") == ["15210", "30000", "999"]
    assert len(set(segment["PRODUCTS"].split(";"))) == 3


def test_top_offer_is_the_smallest_rank():
    before = document(offers=[offer(1, rank=2), offer(2, rank=1)])
    segment = build_segment("app-1", before, document())
    assert segment["TOP_OFFER_BEFORE"] == "2"


def test_top_offer_tie_is_broken_deterministically():
    before = document(offers=[offer(7, "B", rank=1), offer(7, "A", rank=1)])
    segment = build_segment("app-1", before, document())
    assert segment["TOP_OFFER_BEFORE"] == "7"
    assert segment["TOP_OFFER_AFTER"] == ""


def test_reference_product_prefers_the_before_side_top_offer():
    before = document(offers=[offer(111)], trials=[trial(111, pd_model="MASTER_M")])
    after = document(offers=[offer(222)], trials=[trial(222, pd_model="BETA_M")])
    segment = build_segment("app-1", before, after)
    assert segment["REFERENCE_PRODUCT_ID"] == "111"
    assert segment["PD_MODEL_BEFORE"] == "MASTER_M"
    # the reference product does not exist in AFTER: an empty value, never a
    # substitution by another product (PLAN.md §10.2)
    assert segment["PD_MODEL_AFTER"] == ""


def test_reference_product_falls_back_to_after_then_to_the_union():
    after_only = build_segment("app-1", document(), document(offers=[offer(222)],
                                                             trials=[trial(222)]))
    assert after_only["REFERENCE_PRODUCT_ID"] == "222"

    # no offers at all: the first product of the (lexicographically) sorted union
    union_only = build_segment("app-1", document(trials=[trial(30000), trial(999)]),
                               document(trials=[trial(30000), trial(999)]))
    assert union_only["REFERENCE_PRODUCT_ID"] == "30000"


def test_cutoff_and_reject_reason_are_read_per_slice():
    before = document(trials=[trial(1, cutoff="CUT_MASTER", reject="LOW_SCORE")])
    after = document(trials=[trial(1, cutoff="CUT_BETA", reject="")])
    segment = build_segment("app-1", before, after)
    assert (segment["CUTOFF_BEFORE"], segment["CUTOFF_AFTER"]) == ("CUT_MASTER", "CUT_BETA")
    assert (segment["REJECT_REASON_BEFORE"], segment["REJECT_REASON_AFTER"]) == ("LOW_SCORE", "")


def test_score_okb_keeps_the_serialization_contract():
    segment = build_segment("app-1", document(score_okb=710), document(score_okb=None))
    assert segment["SCORE_OKB_BEFORE"] == "710"
    assert segment["SCORE_OKB_AFTER"] == "", "a missing characteristic is empty, not NULL"

    # segments cannot tell "absent" from "null": both are the empty string.
    # The distinction lives in the comparison rows (VALUE_BEFORE is None), which
    # is where the delivered check reads it.
    nulled = build_segment("app-1", {"characteristics": {"score_okb": None}}, {})
    assert nulled["SCORE_OKB_BEFORE"] == ""


def test_empty_document_produces_an_empty_but_complete_segment():
    segment = build_segment("app-1", {}, {})
    assert segment["PRODUCTS"] == ""
    assert segment["TOP_OFFER_BEFORE"] == "" and segment["TOP_OFFER_AFTER"] == ""
    assert segment["DECISION_BEFORE"] == "" and segment["DECISION_CHANGED"] == 0
    assert segment["REFERENCE_PRODUCT_ID"] == ""


def test_product_segments_keep_one_row_per_slice_and_product():
    before = document(offers=[offer(1)], trials=[trial(1, pd_model="M1")])
    after = document(offers=[offer(1, rank=1), offer(2, rank=2)],
                     trials=[trial(1, pd_model="M2"), trial(2)])
    rows = build_product_segments("app-1", before, after)
    assert [(r["source"], r["product_id"]) for r in rows] == [
        ("BEFORE", "1"), ("AFTER", "1"), ("AFTER", "2"),
    ]
    by_key = {(r["source"], r["product_id"]): r for r in rows}
    assert by_key[("BEFORE", "1")]["pd_model"] == "M1"
    assert by_key[("AFTER", "1")]["pd_model"] == "M2"
    assert by_key[("AFTER", "2")]["offer_type"] == "MICRO"
    assert all(r["app_id"] == "app-1" for r in rows)


def test_product_segments_survive_a_slice_without_trials():
    before = document()
    after = document(trials=[trial(5, cutoff="CUT_BETA")])
    rows = build_product_segments("app-1", before, after)
    assert [(r["source"], r["product_id"]) for r in rows] == [("AFTER", "5")]
    assert rows[0]["cutoff"] == "CUT_BETA"
    assert rows[0]["cutoff_value"] == "0.5"
