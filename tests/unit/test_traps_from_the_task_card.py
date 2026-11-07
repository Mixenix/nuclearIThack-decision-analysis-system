"""Coverage for the parsing semantics that a mutation audit found unguarded.

A mutation run showed that several behaviours could be inverted, reordered or
dropped while every test stayed green: bucket-rule ordering and the prefix
boundary, the ``labels=`` branch, the segment fields ``PARTNER_TYPE`` and
``ROUTING_METHOD``, and the ``NaN`` spelling in value serialisation.  Those are
exactly the "traps" the task card warns about, so they get explicit tests here.
"""

from __future__ import annotations

import math

import pytest

from change_analysis.pipeline.core import BucketRule, make_adjuster, serialize
from change_analysis.pipeline.segments import build_segment
from change_analysis.procedures import spr_normalize as normalize


# --- bucketing ---------------------------------------------------------------

def test_the_longest_matching_prefix_wins() -> None:
    """A more specific rule must win regardless of the order it was given in."""
    general = BucketRule("decisionResult", (0.0, 1.0), labels=("low", "high"))
    specific = BucketRule("decisionResult.scoring", (0.0, 0.5), labels=("half", "full"))
    for rules in ((general, specific), (specific, general)):
        adjust = make_adjuster(list(rules))
        assert adjust("decisionResult.scoring.value", "0.25", "number") == "half"


def test_prefix_matches_on_a_segment_boundary_only() -> None:
    """``decisionResult.scor`` must not capture ``decisionResult.scoring``."""
    adjust = make_adjuster([BucketRule("decisionResult.scor", (0.0, 1.0),
                                       labels=("a", "b"))])
    assert adjust("decisionResult.scoring.value", "0.25", "number") == "0.25"
    assert adjust("decisionResult.scor.x", "0.25", "number") == "a"


def test_labels_are_used_when_given_and_edges_otherwise() -> None:
    with_labels = make_adjuster([BucketRule("p", (0.0, 0.5, 1.0),
                                            labels=("низкий", "высокий"))])
    assert with_labels("p", "0.1", "number") == "низкий"
    assert with_labels("p", "0.9", "number") == "высокий"

    without = make_adjuster([BucketRule("p", (0.0, 0.5, 1.0))])
    assert "0.5" in without("p", "0.1", "number")


def test_non_numbers_are_not_bucketed() -> None:
    adjust = make_adjuster([BucketRule("p", (0.0, 1.0), labels=("a", "b"))])
    assert adjust("p", "APPROVE", "string") == "APPROVE"
    assert adjust("p", "", "null") == ""


# --- serialisation -----------------------------------------------------------

def test_numbers_keep_a_stable_representation() -> None:
    assert serialize(1.0)[0] == "1"
    assert serialize(1)[0] == "1"
    assert serialize(0.55)[0] == "0.55"
    assert serialize(-0.0)[0] in ("0", "-0")


def test_special_floats_do_not_crash_and_stay_distinguishable() -> None:
    nan_text, nan_type = serialize(float("nan"))
    inf_text, inf_type = serialize(float("inf"))
    assert nan_type == inf_type == "number"
    assert nan_text.lower() == "nan", nan_text
    assert inf_text.lower() in ("inf", "infinity", "+inf"), inf_text
    assert math.isnan(float(nan_text)) or nan_text.lower() == "nan"


def test_booleans_and_null_are_not_numbers() -> None:
    assert serialize(True) == ("true", "boolean")
    assert serialize(False) == ("false", "boolean")
    assert serialize(None) == ("NULL", "null")


# --- segment layer -----------------------------------------------------------

def _doc(trial: dict) -> dict:
    return {"decisionResult": {"decision": "APPROVE", "product_trials": [trial]}}


def test_partner_type_and_routing_method_survive_into_the_segment() -> None:
    """These two columns are what the analyst filters by; nothing asserted them."""
    trial = {"product_id": 1, "partner_type": "NEW_PARTNER",
             "routing_method": "api", "decision": "APPROVE"}
    segment = build_segment("A1", _doc(trial), _doc(trial))
    assert segment["PARTNER_TYPE"] == "NEW_PARTNER"
    assert segment["ROUTING_METHOD"] == "api"


def test_partner_type_is_collapsed_deterministically_not_merged() -> None:
    """A singular column takes the reference product's value — AFTER first.

    ``PLAN.md`` §10.2.7 allows a sorted union here, but the mart schema defines
    ``PARTNER_TYPE`` as one value that filters compare against, so the union would
    make filtering ambiguous.  The choice is recorded in ``DECISIONS.md``; the full
    picture lives in ``product_segments``, which is tested next.
    """
    before = _doc({"product_id": 1, "partner_type": "NEW_PARTNER",
                   "routing_method": "api"})
    after = _doc({"product_id": 1, "partner_type": "CURRENT",
                  "routing_method": "referal"})
    segment = build_segment("A1", before, after)
    assert segment["PARTNER_TYPE"] == "CURRENT"
    assert segment["ROUTING_METHOD"] == "referal"

    # ... and a value that exists only on the BEFORE side is still reported
    before_only = _doc({"product_id": 1, "partner_type": "NEW_PARTNER"})
    after_empty = _doc({"product_id": 1})
    assert build_segment("A1", before_only, after_empty)["PARTNER_TYPE"] == "NEW_PARTNER"


def test_products_are_the_sorted_union_of_both_slices() -> None:
    before = _doc({"product_id": 2})
    after = _doc({"product_id": 1})
    segment = build_segment("A1", before, after)
    assert segment["PRODUCTS"] == "1;2"


def test_decision_changed_is_an_int_and_follows_the_top_level_decision() -> None:
    before = {"decisionResult": {"decision": "REJECT",
                                 "product_trials": [{"product_id": 1}]}}
    after = {"decisionResult": {"decision": "APPROVE",
                                "product_trials": [{"product_id": 1}]}}
    segment = build_segment("A1", before, after)
    assert segment["DECISION_CHANGED"] == 1
    assert isinstance(segment["DECISION_CHANGED"], int)
    same = build_segment("A1", after, after)
    assert same["DECISION_CHANGED"] == 0


# --- the delivered configuration ---------------------------------------------

def test_the_procedure_keeps_technical_fields_out() -> None:
    assert "appData.request_id" in normalize.DEFAULTS["excluded_paths"]
    assert "appData.createDateTime" in normalize.DEFAULTS["excluded_paths"]


def test_array_rules_cover_the_nine_documented_arrays() -> None:
    paths = {item["path"] for item in normalize.DEFAULTS["arrays"]}
    assert paths == {
        "decisionResult.available_offers",
        "decisionResult.product_trials",
        "decisionResult.product_trials.rules",
        "decisionResult.product_trials.scoring.scoreModels",
        "decisionResult.scoring",
        "decisionResult.scoring.value",
        "ext_data",
        "product_catalog",
        "tariff_matrix",
    }


def test_available_offers_uses_a_composite_key() -> None:
    """product_id alone would merge the standard and the manual offer."""
    for item in normalize.DEFAULTS["arrays"]:
        if item["path"] == "decisionResult.available_offers":
            assert item["mode"] == "field"
            assert item["key_fields"] == ["product_id", "type"]
            return
    pytest.fail("правило для available_offers не найдено")


# --- registered files: listing without execution, containment -----------------

def test_listing_registered_checks_does_not_execute_them(tmp_path, monkeypatch) -> None:
    """A heavy module-level side effect must not run while the list is built."""
    from change_analysis.checks import loader
    from change_analysis.config import load_settings

    script_root = tmp_path / "scripts"
    script_root.mkdir()
    marker = tmp_path / "executed.marker"
    (script_root / "loud_check.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n"
        "def run(mart, params=None):\n    return []\n"
        "CHECK = {'id': 'loud', 'label': 'громкая', 'params': {'a': 1}, 'entrypoint': run}\n",
        encoding="utf-8")
    (script_root / "procedure.py").write_text(
        "PROCEDURE = {'id': 'p', 'stage': 'normalize'}\n", encoding="utf-8")

    settings = load_settings(force=True)
    monkeypatch.setattr(type(settings), "script_root",
                        property(lambda self: script_root), raising=False)

    listed = {spec.id for spec in loader.list_script_checks()}
    assert listed == {"loud"}, listed
    assert not marker.exists(), "файл был исполнен при построении списка"


def test_check_file_outside_script_root_is_refused(tmp_path) -> None:
    from change_analysis.services.errors import InvalidRequest
    from change_analysis.services import procedures

    outside = tmp_path / "outside_check.py"
    outside.write_text("CHECK = {'id': 'x', 'label': 'x', 'params': {}, "
                       "'entrypoint': lambda m, p=None: []}\n", encoding="utf-8")
    with pytest.raises(InvalidRequest):
        procedures._registered_file(str(outside))
