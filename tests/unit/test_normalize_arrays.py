"""Array semantics of PLAN.md §8.2 / §21.1 — the traps the task card warns about.

Every case here is a documented data trap:

* ``available_offers`` is keyed by **(product_id, type)**, so the same product with
  a different offer type is two different entities, not one changing entity;
* a reordered array must not look like a change when the array is keyed by value
  or by field, but **must** look like a change when it is a positional vector;
* a duplicate key is an error with a clear message, never a silent de-duplication;
* an array nobody declared is an error naming its PATH.
"""

from __future__ import annotations

import pytest

from change_analysis.pipeline.core import (
    DEFAULT_ARRAY_SPECS,
    ArraySpec,
    BucketRule,
    NormalizationError,
    compare_app,
    make_adjuster,
    normalize_document,
)

pytestmark = pytest.mark.unit


def offers(*pairs, rank_start: int = 1):
    return [
        {"product_id": product_id, "type": kind, "rank": rank_start + index}
        for index, (product_id, kind) in enumerate(pairs)
    ]


def doc(*, offers_list=None, trials=None, scoring=None, extra=None):
    """Minimal СПР-shaped document."""
    decision = {}
    if offers_list is not None:
        decision["available_offers"] = offers_list
    if trials is not None:
        decision["product_trials"] = trials
    if scoring is not None:
        decision["scoring"] = scoring
    payload = {"decisionResult": decision} if decision else {}
    payload.update(extra or {})
    return payload


def rows_for(document, source="BEFORE", app_id="app-1", **kwargs):
    return normalize_document(document, source, app_id, **kwargs)


def changed_types(before, after):
    return {(r.path, r.array_key): r.change_type for r in compare_app(before, after)}


# ---------------------------------------------------------------------------
# composite keys
# ---------------------------------------------------------------------------


def test_available_offers_is_keyed_by_product_and_type():
    """Trap #2: same product_id, different type = two different entities."""
    document = doc(offers_list=offers((15210, "MICRO"), (15210, "CREDIT")))
    rows = rows_for(document)
    keys = sorted(r.array_key for r in rows if r.path.endswith("available_offers.rank"))
    assert keys == ["15210|CREDIT", "15210|MICRO"]
    assert len(keys) == len(set(keys))


def test_product_catalog_is_keyed_by_mfo_and_product():
    document = {"product_catalog": [
        {"mfo_id": 1, "product_id": 10, "limit": 100},
        {"mfo_id": 2, "product_id": 10, "limit": 200},
    ]}
    rows = rows_for(document)
    limits = {r.array_key: r.value for r in rows if r.path == "product_catalog.limit"}
    assert limits == {"1|10": "100", "2|10": "200"}


def test_element_key_may_itself_be_a_dotted_path():
    """``ext_data`` is keyed by ``body.action`` — a field inside the element."""
    document = {"ext_data": [
        {"body": {"action": "SMS"}, "result": "ok"},
        {"body": {"action": "PUSH"}, "result": "fail"},
    ]}
    rows = rows_for(document)
    results = {r.array_key: r.value for r in rows if r.path == "ext_data.result"}
    assert results == {"SMS": "ok", "PUSH": "fail"}


def test_missing_key_field_is_a_clear_error():
    document = {"ext_data": [{"body": {"wrong": "SMS"}}]}
    with pytest.raises(ValueError) as error:
        rows_for(document)
    assert "ext_data" in str(error.value)
    assert "body.action" in str(error.value)


# ---------------------------------------------------------------------------
# reordering
# ---------------------------------------------------------------------------


def test_field_keyed_array_reorder_is_not_a_change():
    """Trap #3: reordering keyed elements must not produce phantom changes."""
    before = rows_for(doc(offers_list=offers((1, "A"), (2, "B"))))
    after = rows_for(doc(offers_list=list(reversed(offers((1, "A"), (2, "B"))))),
                     source="AFTER")
    compared = compare_app(before, after)
    assert compared, "the comparison produced no rows at all"
    assert {r.change_type for r in compared} == {"SAME"}
    assert all(r.diff_flag == 0 for r in compared)


def test_value_keyed_scalar_array_reorder_is_not_a_change():
    """PLAN.md §21.1 «按值模式的标量数组重排 → 无虚假变更»."""
    spec = ArraySpec("tags", "value")
    before = rows_for({"tags": ["a", "b", "c"]}, array_specs=[spec])
    after = rows_for({"tags": ["c", "a", "b"]}, source="AFTER", array_specs=[spec])
    compared = compare_app(before, after)
    assert compared
    assert {r.change_type for r in compared} == {"SAME"}
    assert {r.array_key for r in compared} == {"a", "b", "c"}


def test_positional_vector_reorder_is_a_change():
    """PLAN.md §21.1: reordering ``scoring.value`` *is* a change (positions matter)."""
    before = rows_for(doc(scoring={"modelName": "M1", "value": [0.1, 0.2]}))
    after = rows_for(doc(scoring={"modelName": "M1", "value": [0.2, 0.1]}), source="AFTER")
    by_key = {r.array_key: r for r in compare_app(before, after)
              if r.path == "decisionResult.scoring.value"}
    assert sorted(by_key) == ["0", "1"]
    assert all(r.change_type == "VALUE_CHANGED" for r in by_key.values())
    assert by_key["0"].value_before == "0.1" and by_key["0"].value_after == "0.2"


# ---------------------------------------------------------------------------
# one predictor, two models
# ---------------------------------------------------------------------------


def test_same_feature_in_two_models_gives_two_rows_with_different_array_key():
    trial = {
        "product_id": 15210,
        "scoring": {"scoreModels": [
            {"modelName": "MODEL_A", "value": 0.31},
            {"modelName": "MODEL_B", "value": 0.32},
        ]},
    }
    rows = rows_for(doc(trials=[trial]))
    values = {r.array_key: r.value for r in rows
              if r.path.endswith("scoreModels.value")}
    assert values == {"15210|MODEL_A": "0.31", "15210|MODEL_B": "0.32"}
    # the trial's own scalars live on the outer array key, the model values on
    # the composite one — that is what keeps the two models apart
    assert {r.array_key for r in rows if r.path.endswith("product_trials.product_id")} \
        == {"15210"}


# ---------------------------------------------------------------------------
# errors, never silence
# ---------------------------------------------------------------------------


def test_unknown_array_is_an_error_naming_the_path():
    with pytest.raises(NormalizationError) as error:
        rows_for({"mystery_array": [1, 2]})
    message = str(error.value)
    assert "mystery_array" in message
    assert "PATH" in message


def test_duplicate_array_key_is_reported_not_deduplicated():
    """Two offers with the same (product_id, type) make the row key ambiguous."""
    document = doc(offers_list=[
        {"product_id": 1, "type": "A", "rank": 1},
        {"product_id": 1, "type": "A", "rank": 2},
    ])
    with pytest.raises(NormalizationError) as error:
        rows_for(document)
    message = str(error.value)
    assert "duplicate row key" in message
    assert "PATH=" in message and "ARRAY_KEY=" in message
    assert "app-1" in message and "BEFORE" in message


# ---------------------------------------------------------------------------
# technical fields
# ---------------------------------------------------------------------------


def test_technical_fields_are_excluded_by_default_and_appear_when_not_excluded():
    document = {"appData": {"request_id": "r-1", "createDateTime": "2026-01-01",
                            "application_id": "app-1"}}
    paths = {r.path for r in rows_for(document)}
    assert "appData.request_id" not in paths
    assert "appData.createDateTime" not in paths
    assert "appData.application_id" in paths

    raw_paths = {r.path for r in rows_for(document, excluded_paths=())}
    assert "appData.request_id" in raw_paths
    assert "appData.createDateTime" in raw_paths


def test_request_id_only_difference_produces_no_substantive_change():
    """PLAN.md §21.1 row 1: only request_id/createDateTime differ ⇒ nothing changed."""
    before = rows_for({"appData": {"request_id": "r-1", "createDateTime": "1",
                                   "application_id": "app-1"}})
    after = rows_for({"appData": {"request_id": "r-2", "createDateTime": "2",
                                  "application_id": "app-1"}}, source="AFTER")
    compared = compare_app(before, after)
    assert compared
    assert {r.change_type for r in compared} == {"SAME"}


def test_removing_the_exclusion_makes_the_difference_appear():
    """PLAN.md §21.1 row 2: drop the technical exclusion ⇒ the difference shows up."""
    before = rows_for({"appData": {"request_id": "r-1"}}, excluded_paths=())
    after = rows_for({"appData": {"request_id": "r-2"}}, source="AFTER", excluded_paths=())
    compared = compare_app(before, after)
    assert [r.change_type for r in compared] == ["VALUE_CHANGED"]
    assert compared[0].diff_flag == 1


# ---------------------------------------------------------------------------
# bucketing
# ---------------------------------------------------------------------------


def test_value_changed_within_one_bucket_still_has_diff_flag_1():
    """СХЕМА_ВИТРИНЫ: DIFF_FLAG is computed on VALUE, never on the bucket."""
    adjust = make_adjuster([BucketRule("characteristics.score_okb",
                                       (0.0, 300.0, 600.0, 1001.0))])
    before = rows_for({"characteristics": {"score_okb": 0.42}}, adjust=adjust)
    after = rows_for({"characteristics": {"score_okb": 0.58}}, source="AFTER", adjust=adjust)
    (row,) = compare_app(before, after)
    assert row.change_type == "VALUE_CHANGED"
    assert row.diff_flag == 1
    assert row.adjusted_before == row.adjusted_after == "(0 - 300]"
    assert row.value_before != row.value_after


def test_bucket_label_is_identical_for_both_slices():
    """СХЕМА_ВИТРИНЫ: the bucket boundaries are the same for BEFORE and AFTER."""
    adjust = make_adjuster([BucketRule("characteristics.score_okb", (0.0, 500.0, 700.0, 1001.0))])
    left = rows_for({"characteristics": {"score_okb": 710}}, adjust=adjust)
    right = rows_for({"characteristics": {"score_okb": 710}}, source="AFTER", adjust=adjust)
    assert left[0].adjusted_value == right[0].adjusted_value == "(700 - 1001]"


@pytest.mark.parametrize(
    "value, expected",
    [(0.0, "<= 0"), (0.3, "(0 - 0.3]"), (0.55, "(0.3 - 0.6]"), (5.0, "> 1")],
)
def test_bucket_boundaries(value, expected):
    rule = BucketRule("x.y", (0.0, 0.3, 0.6, 1.0))
    assert rule.label_for(value) == expected


def test_non_numeric_value_keeps_the_bucket_function_harmless():
    adjust = make_adjuster([BucketRule("characteristics.score_okb", (0.0, 1.0))])
    assert adjust("characteristics.score_okb", "NULL", "null") == "NULL"
    assert adjust("characteristics.score_okb", "abc", "string") == "abc"
    assert adjust("other.path", "0.5", "number") == "0.5"


# ---------------------------------------------------------------------------
# the "nothing changed" dataset
# ---------------------------------------------------------------------------


def test_identical_documents_keep_every_same_row():
    """PLAN.md §21.1: an unchanged dataset stays all-zero in diffs, SAME rows kept."""
    document = doc(offers_list=offers((1, "A")), trials=[{"product_id": 1, "decision": "x"}],
                   scoring={"modelName": "M", "value": [0.1]})
    before = rows_for(document)
    after = rows_for(document, source="AFTER")
    compared = compare_app(before, after)
    assert len(compared) == len(before)
    assert {r.change_type for r in compared} == {"SAME"}
    assert not [r for r in compared if r.diff_flag == 1]


def test_default_specs_are_the_documented_ones():
    """The array table is user configuration; these entries are the shipped defaults."""
    paths = {spec.path: spec for spec in DEFAULT_ARRAY_SPECS}
    assert paths["decisionResult.available_offers"].key_fields == ("product_id", "type")
    assert paths["decisionResult.product_trials"].key_fields == ("product_id",)
    assert paths["decisionResult.product_trials.rules"].key_fields == ("name",)
    assert paths["ext_data"].key_fields == ("body.action",)
    assert paths["decisionResult.scoring.value"].mode == "index"
