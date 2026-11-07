"""Regressions for the defects found by the red-team audit (2026-11-07).

Each test reproduces one reported problem at the narrowest layer that owns it:
a dataset manifest must not reach outside its root, the XML reader must not treat
an ordinary ``type`` attribute as a type marker, and check-result validation must
not silently "repair" numbers or accept applications from another run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from change_analysis.checks.loader import CheckError, validate_rows
from change_analysis.procedures import spr_normalize_xml as xml_reader
from change_analysis.services.datasets import precheck_dataset


def _dataset(tmp_path: Path, before_rel: str, after_rel: str) -> Path:
    """A manifest whose slices point wherever the test says — that is the attack."""
    root = tmp_path / "data"
    run = root / "RUN"
    (run / "RESULTS" / "BEFORE").mkdir(parents=True)
    (run / "RESULTS" / "AFTER").mkdir(parents=True)
    (run / "RESULTS" / "BEFORE" / "I1.json").write_text("{}", encoding="utf-8")
    (run / "RESULTS" / "AFTER" / "I1.json").write_text("{}", encoding="utf-8")
    (run / "manifest.json").write_text(json.dumps({
        "run_id": "RUN",
        "srez": {"BEFORE": {"dir": before_rel}, "AFTER": {"dir": after_rel}},
    }), encoding="utf-8")
    return root


def test_manifest_cannot_point_at_a_sibling_with_a_shared_prefix(tmp_path: Path) -> None:
    """``data-leak`` shares a string prefix with ``data`` — containment must be paths."""
    # two levels up is a genuine escape: data/RUN/../../data-leak
    root = _dataset(tmp_path, "../../data-leak/BEFORE", "../../data-leak/AFTER")
    leak = tmp_path / "data-leak"
    (leak / "BEFORE").mkdir(parents=True)
    (leak / "AFTER").mkdir(parents=True)
    (leak / "BEFORE" / "I1.json").write_text("{}", encoding="utf-8")
    (leak / "AFTER" / "I1.json").write_text("{}", encoding="utf-8")

    result = precheck_dataset("RUN", str(root), "*.json")

    assert result["ok"] is False
    assert any("escapes" in error for error in result["errors"]), result["errors"]


def test_normal_manifest_still_passes(tmp_path: Path) -> None:
    root = _dataset(tmp_path, "RESULTS/BEFORE", "RESULTS/AFTER")
    result = precheck_dataset("RUN", str(root), "*.json")
    assert result["ok"] is True, result["errors"]
    assert result["pairs"] == 1


def test_plain_type_attribute_is_data_not_a_type_marker(tmp_path: Path) -> None:
    path = tmp_path / "d.xml"
    path.write_text('<r><offer type="loan">42</offer></r>', encoding="utf-8")
    assert xml_reader.read_document(path) == {"offer": {"@type": "loan", "#text": 42}}

    path.write_text('<r><offer type="null"><amount>42</amount></offer></r>', encoding="utf-8")
    # the attribute is preserved and the content is NOT thrown away
    assert xml_reader.read_document(path) == {"offer": {"@type": None, "amount": 42}}


def test_namespaced_type_marker_still_works(tmp_path: Path) -> None:
    path = tmp_path / "d.xml"
    path.write_text(
        '<r xmlns:t="urn:change-analysis:types"><a t:type="string">42</a>'
        '<b t:type="null"/></r>', encoding="utf-8")
    assert xml_reader.read_document(path) == {"a": "42", "b": None}


def test_validation_rejects_fractional_counts() -> None:
    row = {"LABEL": "L", "SOURCE": "AFTER", "CNT": 1.9, "TOTAL": 2.9, "METRIC": 0.9,
           "APP_IDS": [], "RESULT": "FAIL", "CONFIDENCE": "HIGH"}
    with pytest.raises(CheckError):
        validate_rows([row], "run")


def test_validation_rejects_app_ids_from_another_run() -> None:
    row = {"LABEL": "L", "SOURCE": "AFTER", "CNT": 1, "TOTAL": 2, "METRIC": 0.5,
           "APP_IDS": ["NOT_IN_THIS_RUN"], "RESULT": "FAIL", "CONFIDENCE": "HIGH"}
    with pytest.raises(CheckError):
        validate_rows([row], "run", known_app_ids={"REAL_APP"})


def test_validation_accepts_a_correct_row() -> None:
    row = {"LABEL": "L", "SOURCE": "AFTER", "CNT": 1, "TOTAL": 2, "METRIC": 0.5,
           "APP_IDS": ["REAL_APP"], "RESULT": "FAIL", "CONFIDENCE": "HIGH"}
    assert len(validate_rows([row], "run", known_app_ids={"REAL_APP"})) == 1
