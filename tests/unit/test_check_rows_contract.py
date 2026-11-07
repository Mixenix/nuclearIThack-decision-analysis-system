"""The check contract of ``checks/README.md`` — rows, errors and edge conditions.

Two different things are verified here:

* the *contract* the application enforces on user code (``validate_rows``,
  ``load_from_source``): a broken check must produce a clear ``CheckError`` and a
  technical failure must never be dressed up as a business ``FAIL``;
* the behaviour of the five delivered files on hand-built data, including the
  division-by-zero corner («нет ни одной одобренной заявки» — PLAN.md §21.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from change_analysis.checks.loader import (
    CheckError,
    list_builtin_checks,
    load_from_source,
    validate_rows,
)

from qa_support import StubMart, segment_row

pytestmark = pytest.mark.unit

DELIVERED_IDS = [
    "ar_drop_by_product",
    "attribute_new_loss",
    "external_score_drift",
    "stop_factor_disappeared",
    "target_model_changed",
]


def legal_row(**overrides):
    row = {
        "LABEL": "L", "SOURCE": "AFTER", "ARGUMENT": "A", "CNT": 1, "TOTAL": 2,
        "METRIC": 0.5, "APP_IDS": ["app-1"], "RESULT": "FAIL", "CONFIDENCE": "HIGH",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# row validation
# ---------------------------------------------------------------------------


def test_legal_row_is_normalised_not_just_accepted():
    """CNT/TOTAL are coerced to int; METRIC must already be a number, because a
    string metric in a report is a silent precision lie."""
    (row,) = validate_rows([legal_row(CNT="3", TOTAL="4")], "run")
    assert row["CNT"] == 3 and row["TOTAL"] == 4
    assert row["METRIC"] == 0.5
    assert isinstance(row["METRIC"], float)
    assert row["APP_IDS"] == ["app-1"]


def test_empty_result_is_legal():
    """«Пустой список — законный результат „выполнено, 0 строк“»."""
    assert validate_rows([], "run") == []
    assert validate_rows(None, "run") == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"SOURCE": "OTHER"}, "SOURCE"),
        ({"RESULT": "OK"}, "RESULT"),
        ({"CONFIDENCE": "MEDIUM"}, "CONFIDENCE"),
        ({"CNT": -1}, "CNT/TOTAL"),
        ({"TOTAL": -5}, "CNT/TOTAL"),
        ({"APP_IDS": "app-1"}, "APP_IDS"),
        ({"METRIC": "half"}, "METRIC"),
    ],
)
def test_illegal_row_names_the_offending_field(overrides, expected):
    with pytest.raises(CheckError) as error:
        validate_rows([legal_row(**overrides)], "run")
    assert expected in str(error.value)


@pytest.mark.parametrize("missing", ["LABEL", "SOURCE", "RESULT", "CONFIDENCE"])
def test_missing_required_field_is_named(missing):
    row = legal_row()
    row.pop(missing)
    with pytest.raises(CheckError) as error:
        validate_rows([row], "run")
    assert missing in str(error.value)


def test_a_single_dict_instead_of_a_list_is_refused():
    with pytest.raises(CheckError) as error:
        validate_rows(legal_row(), "run")
    assert "list" in str(error.value)


def test_rows_must_be_dicts():
    with pytest.raises(CheckError) as error:
        validate_rows(["not-a-row"], "run")
    assert "dict" in str(error.value)


def test_app_ids_are_restricted_to_the_run_when_the_caller_knows_them():
    with pytest.raises(CheckError) as error:
        validate_rows([legal_row(APP_IDS=["app-1", "app-999"])], "run",
                      known_app_ids={"app-1"})
    assert "outside this run" in str(error.value)
    assert "app-999" in str(error.value)


# ---------------------------------------------------------------------------
# loading user code
# ---------------------------------------------------------------------------


def test_check_without_CHECK_object_is_refused():
    with pytest.raises(CheckError) as error:
        load_from_source("x = 1\n", "unit")
    assert "CHECK" in str(error.value)


def test_check_missing_a_key_is_refused():
    source = "CHECK = {'id': 'x', 'label': 'X', 'entrypoint': lambda m, p: []}\n"
    with pytest.raises(CheckError) as error:
        load_from_source(source, "unit")
    assert "params" in str(error.value)


def test_entrypoint_must_be_callable():
    source = ("CHECK = {'id': 'x', 'label': 'X', 'params': {}, 'entrypoint': 42}\n")
    with pytest.raises(CheckError) as error:
        load_from_source(source, "unit")
    assert "callable" in str(error.value)


def test_syntax_error_is_reported_as_a_check_error():
    with pytest.raises(CheckError) as error:
        load_from_source("def broken(:\n", "unit")
    assert "syntax error" in str(error.value).lower()


# ---------------------------------------------------------------------------
# the five delivered files
# ---------------------------------------------------------------------------


def test_all_five_delivered_checks_load_from_the_task_package():
    specs = list_builtin_checks()
    assert sorted(spec.id for spec in specs) == DELIVERED_IDS
    checks_dir = Path(specs[0].source_ref).parent
    for spec in specs:
        assert spec.label, f"{spec.id} has no label"
        assert isinstance(spec.params, dict) and spec.params, f"{spec.id} has no params"
        assert callable(spec.entrypoint)
        assert spec.source_kind == "file"
        assert Path(spec.source_ref).parent == checks_dir
        assert Path(spec.source_ref).name.startswith("check_")


def test_delivered_checks_survive_an_empty_mart_without_dividing_by_zero():
    """PLAN.md §21.1 «нет ни одной одобренной заявки → не появляется деление на ноль»."""
    empty = StubMart(rows=[], segments=[])
    for spec in list_builtin_checks():
        produced = spec.entrypoint(empty, dict(spec.params))
        assert isinstance(produced, list), f"{spec.id} did not return a list"
        validated = validate_rows(produced, "run")   # must not raise
        for row in validated:
            assert row["CNT"] == 0
            assert row["TOTAL"] == 0
            assert row["RESULT"] in ("PASS", "FAIL")
            assert row["CONFIDENCE"] in ("HIGH", "LOW")


def check_by_id(check_id):
    return {spec.id: spec for spec in list_builtin_checks()}[check_id]


def test_approval_rate_drop_is_relative_to_the_before_slice():
    spec = check_by_id("ar_drop_by_product")
    segments = [
        segment_row("run", f"app-{i}", decision_before="APPROVE", decision_after="REJECT",
                    products="15210")
        for i in range(3)
    ] + [segment_row("run", "app-ok", products="15210")]
    (row,) = spec.entrypoint(StubMart(segments=segments), dict(spec.params))
    assert row["ARGUMENT"] == "15210"
    assert (row["CNT"], row["TOTAL"]) == (3, 4)
    assert row["METRIC"] == 0.75
    assert row["RESULT"] == "FAIL"
    assert row["CONFIDENCE"] == "LOW", "fewer than low_confidence_cnt hits ⇒ LOW"


def test_approval_rate_drop_without_any_approval_is_zero_not_a_crash():
    spec = check_by_id("ar_drop_by_product")
    segments = [segment_row("run", "app-1", decision_before="REJECT",
                            decision_after="REJECT", products="15210")]
    (row,) = spec.entrypoint(StubMart(segments=segments), dict(spec.params))
    assert row["METRIC"] == 0.0
    assert row["RESULT"] == "PASS"


def test_attribute_new_loss_reports_paths_and_falls_back_to_pass():
    spec = check_by_id("attribute_new_loss")
    rows = [
        {"APP_ID": f"app-{i}", "PATH": "appData.customer.segment", "ARRAY_KEY": "",
         "DIFF_FLAG": 1, "CHANGE_TYPE": "ATTRIBUTE_NEW", "KEY": "segment"}
        for i in range(6)
    ]
    segments = [segment_row("run", f"app-{i}") for i in range(6)]
    (row,) = spec.entrypoint(StubMart(rows=rows, segments=segments), dict(spec.params))
    assert row["ARGUMENT"] == "appData.customer.segment"
    assert row["SOURCE"] == "AFTER"
    assert row["RESULT"] == "FAIL"
    assert row["CONFIDENCE"] == "HIGH"

    empty_row = spec.entrypoint(StubMart(), dict(spec.params))[0]
    assert empty_row["RESULT"] == "PASS" and empty_row["CNT"] == 0


def test_stop_factor_separates_a_removed_rule_from_a_rule_that_stopped_hitting():
    spec = check_by_id("stop_factor_disappeared")
    base = {"LABEL": "x", "SOURCE": "AFTER", "PATH": "decisionResult.product_trials.rules.hit",
            "DIFF_FLAG": 1, "KEY": "hit"}
    rows = [
        dict(base, APP_ID="app-1", ARRAY_KEY="15210|AGE_LOW", CHANGE_TYPE="ATTRIBUTE_LOST",
             VALUE_BEFORE="true", VALUE_AFTER=None),
        dict(base, APP_ID="app-2", ARRAY_KEY="15210|STOP_FACTOR",
             CHANGE_TYPE="VALUE_CHANGED", VALUE_BEFORE="true", VALUE_AFTER="false"),
        dict(base, APP_ID="app-3", ARRAY_KEY="15210|NOISE",
             CHANGE_TYPE="VALUE_CHANGED", VALUE_BEFORE="false", VALUE_AFTER="true"),
    ]
    segments = [segment_row("run", f"app-{i}") for i in (1, 2, 3)]
    result = spec.entrypoint(StubMart(rows=rows, segments=segments), dict(spec.params))
    arguments = {row["ARGUMENT"]: row for row in result}
    assert arguments["AGE_LOW"]["CNT"] == 1       # rule removed from the strategy
    assert arguments["STOP_FACTOR"]["CNT"] == 1   # rule stopped firing
    assert "NOISE" not in arguments, "false → true is the opposite event"


def test_target_model_changed_counts_the_pair_of_models():
    spec = check_by_id("target_model_changed")
    segments = [
        segment_row("run", "app-1", pd_model_before="MODEL_A", pd_model_after="MODEL_B"),
        segment_row("run", "app-2", pd_model_before="MODEL_A", pd_model_after="MODEL_B",
                    cutoff_before="CUT_A", cutoff_after="CUT_B"),
        segment_row("run", "app-3", pd_model_before="MODEL_A", pd_model_after="MODEL_A"),
    ]
    (row,) = spec.entrypoint(StubMart(segments=segments), dict(spec.params))
    assert row["ARGUMENT"] == "MODEL_A -> MODEL_B"
    assert row["CNT"] == 2 and row["TOTAL"] == 3
    assert row["RESULT"] == "FAIL"


def test_external_score_drift_flags_false_alarms_and_predictor_mismatch():
    spec = check_by_id("external_score_drift")
    score_path = "characteristics.score_okb"
    feature_path = "decisionResult.scoring.features.score_okb"
    rows = [
        {"APP_ID": "app-1", "PATH": score_path, "ARRAY_KEY": "", "DIFF_FLAG": 1,
         "CHANGE_TYPE": "VALUE_CHANGED", "VALUE_BEFORE": "700", "VALUE_AFTER": "710",
         "KEY": "score_okb"},
        {"APP_ID": "app-1", "PATH": feature_path, "ARRAY_KEY": "", "DIFF_FLAG": 1,
         "CHANGE_TYPE": "VALUE_CHANGED", "VALUE_BEFORE": "700", "VALUE_AFTER": "999",
         "KEY": "score_okb"},
        {"APP_ID": "app-2", "PATH": score_path, "ARRAY_KEY": "", "DIFF_FLAG": 1,
         "CHANGE_TYPE": "VALUE_CHANGED", "VALUE_BEFORE": "700", "VALUE_AFTER": "700",
         "KEY": "score_okb"},
    ]
    segments = [segment_row("run", "app-1", decision_before="APPROVE",
                            decision_after="REJECT"),
                segment_row("run", "app-2")]
    first, second = spec.entrypoint(StubMart(rows=rows, segments=segments), dict(spec.params))
    assert first["ARGUMENT"] == score_path
    assert first["CNT"] == 1 and first["RESULT"] == "FAIL"
    assert second["ARGUMENT"] == "предиктор != источник"
    assert second["CNT"] == 1 and second["RESULT"] == "FAIL"
