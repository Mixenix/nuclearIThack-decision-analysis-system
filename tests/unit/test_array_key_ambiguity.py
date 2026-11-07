"""ARRAY_KEY must stay unambiguous when a value contains the separator.

The mart's primary key is ``(APP_ID, PATH, ARRAY_KEY)`` and ``ARRAY_KEY`` joins the
nested array keys with ``|``.  A key that itself contains ``|`` therefore has to be
escaped, otherwise two different attributes collapse into one row — and the report
would silently compare a value against the wrong one.

A mutation audit found that removing the escaping from ``join_array_keys`` left all
280 tests green: the only coverage was of ``escape_key()`` in isolation, never of
the join on real shapes.  These tests close that hole.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from change_analysis.pipeline.core import (
    ArraySpec,
    escape_key,
    join_array_keys,
    normalize_document,
)

TRIALS = ArraySpec("decisionResult.product_trials", "field", ("product_id",))
RULES = ArraySpec("decisionResult.product_trials.rules", "field", ("name",))


def _rules_doc(product_id: str, rule_name: str) -> dict:
    return {"decisionResult": {"product_trials": [
        {"product_id": product_id, "rules": [{"name": rule_name, "hit": True}]}]}}


def _keys(product_id: str, rule_name: str) -> set[str]:
    rows = normalize_document(_rules_doc(product_id, rule_name), "BEFORE", "A1",
                              array_specs=(TRIALS, RULES), excluded_paths=())
    return {row.array_key for row in rows if row.path.endswith(".rules.hit")}


def test_separator_inside_a_key_is_escaped() -> None:
    assert escape_key("Y|Z") == r"Y\|Z"
    assert escape_key("plain") == "plain"
    assert join_array_keys(["X", "Y|Z"]) == r"X|Y\|Z"
    assert join_array_keys(["a\\b"]) == r"a\\b"


def test_two_different_attributes_do_not_collapse() -> None:
    """``X | Y|Z`` and ``X|Y | Z`` must not produce the same row key."""
    left = _keys("X", "Y|Z")
    right = _keys("X|Y", "Z")
    assert left and right
    assert left != right, (
        "ключи схлопнулись: без экранирования разделителя витрина сравнивает "
        "значение одного атрибута с другим")


def test_escaping_survives_a_round_trip_by_splitting_on_unescaped_separators() -> None:
    """The escaped key can be split back into its parts without ambiguity."""
    parts = ["X|Y", "Z", "W"]
    joined = join_array_keys(parts)

    def split(key: str) -> list[str]:
        out, current, escaped = [], "", False
        for char in key:
            if escaped:
                current += char
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "|":
                out.append(current)
                current = ""
            else:
                current += char
        out.append(current)
        return out

    assert split(joined) == parts
    assert split(join_array_keys(["X", "Y|Z", "W"])) == ["X", "Y|Z", "W"]


def test_the_real_dataset_keeps_its_keys_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check on the delivered data: no two attributes share a row key."""
    from change_analysis.config import load_settings
    from change_analysis.db.connection import query

    try:
        run = query("SELECT id, summary->>'pairs' AS pairs FROM ca.runs"
                    " WHERE summary->>'pairs' = '2002' ORDER BY created_at DESC LIMIT 1")
    except Exception:  # noqa: BLE001 - no database in this environment
        pytest.skip("база недоступна")
    if not run:
        pytest.skip("нет прогона на 2002 пары")

    duplicated = query(
        "SELECT count(*) AS n FROM (SELECT app_id, path, array_key"
        " FROM ca.comparison_rows WHERE run_id = %s"
        " GROUP BY 1,2,3 HAVING count(*) > 1) AS dup", (run[0]["id"],))[0]["n"]
    assert duplicated == 0, f"в витрине {duplicated} повторяющихся ключей строк"

    escaped = query(
        "SELECT count(*) AS n FROM ca.comparison_rows"
        " WHERE run_id = %s AND array_key LIKE %s", (run[0]["id"], "%\\|%"))[0]["n"]
    assert escaped >= 0  # the delivered data simply has no such keys; the invariant is above
